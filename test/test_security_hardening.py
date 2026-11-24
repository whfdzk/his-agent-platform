# 安全加固相关的确定性单测。
#
# 覆盖本轮修复的四类问题：
# 1. 预约域越权：无 token 必须 401，且身份只能来自 token（body 里的 patient_id 无效）
# 2. 登录限流：Redis 限流器拒绝时返回 429，且查询 DB 之前就被拦住
# 3. NL2SQL 注入面：非白名单表 / 多语句 / 系统目录 / 注释 一律拒绝
# 4. LLM 出网脱敏：手机号、身份证不得随 prompt 出网；年龄区间化

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import rate_limit
from src.api.rate_limit import enforce_login_rate
from src.api.routers import appointment as appointment_router
from src.api.routers import metrics as metrics_router
from src.core.exceptions import BizException, register_exception_handlers
from src.core.llm_guard import age_band, scrub_for_llm
from src.knowledge.nl2sql import _validate_sql
from src.utils.jwt_utils import encode_jwt


# ── 预约域鉴权 ────────────────────────────────────────────────────────────

class _ApptDB:
    """只实现预约路由用到的最小集合。"""

    def __init__(self) -> None:
        self.items: dict[int, SimpleNamespace] = {}
        self._next = 1

    def add(self, obj) -> None:
        obj.id = self._next
        self._next += 1
        self.items[obj.id] = obj

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        return None

    async def execute(self, _stmt):
        # 医生目录查询期望 (profile, name) 二元组；这个 fake 没有医生表，返回空，
        # resolve_doctor 会按"无候选"走默认分配，不影响预约鉴权断言。
        rows = [] if "clinician" in str(_stmt).lower() else list(self.items.values())
        return SimpleNamespace(
            scalars=lambda: SimpleNamespace(all=lambda: rows),
            scalar=lambda: len(rows),
            scalar_one_or_none=lambda: (rows[0] if rows else None),
            all=lambda: rows,
        )

    async def get(self, _model, pk):
        return self.items.get(pk)


@pytest.fixture
def appt_client():
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(appointment_router.router)
    db = _ApptDB()
    app.dependency_overrides[appointment_router.get_db] = lambda: db
    return TestClient(app), db


def _auth(patient_id: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {encode_jwt({'sub': patient_id, 'patient_id': int(patient_id)})}"}


def test_appointments_require_auth(appt_client):
    client, _ = appt_client
    assert client.get("/api/v1/appointments").status_code == 401
    assert client.post("/api/v1/appointments", json={"department": "内科"}).status_code == 401
    assert client.get("/api/v1/appointments/1").status_code == 401


def test_appointment_identity_comes_from_token(appt_client):
    client, db = appt_client
    resp = client.post(
        "/api/v1/appointments",
        headers=_auth("101"),
        json={"department": "内科", "doctor_name": "李医生", "patient_id": 999, "user_id": "999"},
    )
    assert resp.status_code == 200
    assert resp.json()["appointment"]["patient_id"] == 101
    assert list(db.items.values())[0].patient_id == 101


def test_cannot_read_or_cancel_others_appointment(appt_client):
    client, _ = appt_client
    created = client.post(
        "/api/v1/appointments", headers=_auth("101"), json={"department": "内科"},
    ).json()["appointment"]
    appt_id = created["id"]

    # 他人访问：返回 404（不泄露 ID 是否存在）
    assert client.get(f"/api/v1/appointments/{appt_id}", headers=_auth("202")).status_code == 404
    assert client.patch(
        f"/api/v1/appointments/{appt_id}/cancel", headers=_auth("202"),
    ).status_code == 404

    # 本人可以访问并取消
    assert client.get(f"/api/v1/appointments/{appt_id}", headers=_auth("101")).status_code == 200
    resp = client.patch(f"/api/v1/appointments/{appt_id}/cancel", headers=_auth("101"))
    assert resp.status_code == 200
    assert resp.json()["appointment"]["status"] == "cancelled"


# ── 登录限流 ──────────────────────────────────────────────────────────────

async def test_login_rate_limit_raises_429(monkeypatch):
    calls: list[str] = []

    async def _deny(key, **_kwargs):
        calls.append(key)
        return False

    monkeypatch.setattr(rate_limit, "sliding_window_allow", _deny)
    with pytest.raises(BizException) as exc:
        await enforce_login_rate("13800138000", "1.2.3.4")
    assert exc.value.status_code == 429
    # 账号被 hash 后入 key：手机号不能出现在 Redis key / 慢日志里
    assert any(k.startswith("rl:login:ip:") for k in calls)
    assert all("13800138000" not in k for k in calls)


async def test_login_rate_limit_is_fail_closed(monkeypatch):
    async def _boom(*_args, **_kwargs):
        raise RuntimeError("redis down")

    monkeypatch.setattr(rate_limit, "sliding_window_allow", _boom)
    with pytest.raises(BizException) as exc:
        await enforce_login_rate("13800138000", "1.2.3.4")
    assert exc.value.status_code == 503


# ── NL2SQL 校验 ───────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM admins",
        "SELECT password_hash FROM admins WHERE id=1",
        "SELECT * FROM patients",
        "SELECT 1; SELECT 2",
        "SELECT * FROM departments -- x",
        "SELECT * FROM information_schema.tables",
        "SELECT pg_sleep(5)",
        "DELETE FROM consultations",
        "SELECT * FROM unknown_table",
    ],
)
def test_nl2sql_rejects_dangerous_sql(sql):
    ok, _ = _validate_sql(sql)
    assert ok is False


def test_nl2sql_allows_whitelisted_query_and_forces_limit():
    ok, sql = _validate_sql("SELECT name FROM departments")
    assert ok is True
    assert sql.upper().endswith("LIMIT 100")

    ok, sql = _validate_sql("SELECT d.name FROM departments d JOIN diseases x ON x.id=d.id LIMIT 5000")
    assert ok is True
    assert "LIMIT 100" in sql.upper()


# ── LLM 出网脱敏 ──────────────────────────────────────────────────────────

def test_scrub_for_llm_removes_identifiers():
    raw = "我叫张三，手机13800138000，身份证110101199001011234，邮箱a@b.com，最近头痛"
    cleaned = scrub_for_llm(raw)
    assert "13800138000" not in cleaned
    assert "110101199001011234" not in cleaned
    assert "a@b.com" not in cleaned
    # 临床语义必须保留
    assert "头痛" in cleaned


def test_age_band():
    assert age_band(63) == "60-69岁"
    assert age_band(28) == "20-29岁"
    assert age_band(95) == "90岁以上"
    assert age_band(None) is None


# ── 诊断端点 ──────────────────────────────────────────────────────────────

def test_llm_test_endpoint_requires_admin():
    """探活端点无鉴权时等于“免费调模型 + 探测内网网关”，必须挡住匿名访问。"""
    from src.api.routers import api as api_router

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(api_router.router)
    client = TestClient(app)
    assert client.get("/medical/llm/test").status_code == 401
    # 患者 token 也不行（需要 role=admin）
    patient_token = encode_jwt({"sub": "101", "patient_id": 101})
    assert client.get(
        "/medical/llm/test", headers={"Authorization": f"Bearer {patient_token}"},
    ).status_code == 403


# ── 监控端点访问控制 ──────────────────────────────────────────────────────

def test_metrics_requires_token_when_configured(monkeypatch):
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(metrics_router.router)

    monkeypatch.setattr(
        metrics_router, "get_settings",
        lambda: SimpleNamespace(APP_ENV="prod", METRICS_TOKEN="s3cret"),
    )
    client = TestClient(app)
    assert client.get("/metrics").status_code == 403
    assert client.get("/metrics", headers={"X-Metrics-Token": "wrong"}).status_code == 403
    # 正确令牌可以正常抓取（200 或 503 取决于连接池状态，这里只验证不因鉴权被拒）
    assert client.get("/metrics", headers={"X-Metrics-Token": "s3cret"}).status_code != 403


def test_metrics_denied_outside_dev_without_token(monkeypatch):
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(metrics_router.router)
    monkeypatch.setattr(
        metrics_router, "get_settings",
        lambda: SimpleNamespace(APP_ENV="prod", METRICS_TOKEN=""),
    )
    assert TestClient(app).get("/metrics").status_code == 403
