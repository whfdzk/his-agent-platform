# 登出链路的确定性单测（不依赖真实 Redis）。
#
# 回归点：登出此前只清前端 localStorage，服务端 token 仍然有效到自然过期。
# 现在的契约：
# 1. POST /auth/logout 把当前 token 的 jti 写入撤销名单，同一 token 再访问 /me 必须 401；
# 2. 登出接口幂等：无 token / 非法 token 也返回 204，不能让前端卡在已登录态；
# 3. all_devices=true 时该账号此前签发的 token 一并失效，且不影响其他账号。

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.deps import get_current_admin
from src.api.routers import admin_auth as admin_auth_router
from src.api.routers import auth as auth_router
from src.core.exceptions import BizException, register_exception_handlers
from src.utils import token_revocation as revocation
from src.utils.jwt_utils import encode_jwt


class _RevocationStore:
    """内存版的撤销名单，语义与 token_revocation 保持一致。"""

    def __init__(self) -> None:
        self.revoked_jtis: set[str] = set()
        self.cutoffs: dict[tuple[str, str], float] = {}

    def install(self, monkeypatch) -> None:
        async def is_jti_revoked(jti: str) -> bool:
            return jti in self.revoked_jtis

        async def revoke_jti(jti: str, _ttl_seconds: int) -> bool:
            self.revoked_jtis.add(jti)
            return True

        async def is_session_revoked(scope: str, subject: str, issued_at: float | None) -> bool:
            cutoff = self.cutoffs.get((scope, subject))
            return cutoff is not None and issued_at is not None and issued_at <= cutoff

        async def revoke_session_before(scope: str, subject: str, cutoff: float) -> bool:
            key = (scope, subject)
            self.cutoffs[key] = max(cutoff, self.cutoffs.get(key, 0.0))
            return True

        monkeypatch.setattr(revocation, "is_jti_revoked", is_jti_revoked)
        monkeypatch.setattr(revocation, "revoke_jti", revoke_jti)
        monkeypatch.setattr(revocation, "is_session_revoked", is_session_revoked)
        monkeypatch.setattr(revocation, "revoke_session_before", revoke_session_before)


@pytest.fixture
def store(monkeypatch) -> _RevocationStore:
    s = _RevocationStore()
    s.install(monkeypatch)
    return s


class _FakeDB:
    """只实现 /me 用到的 db.get。"""

    async def get(self, _model, pk):
        return SimpleNamespace(
            id=pk, name="测试患者", phone="13900001111", gender="男", age=30, enabled=True,
        )


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(auth_router.router)
    app.dependency_overrides[auth_router.get_db] = lambda: _FakeDB()
    return TestClient(app)


def _token(patient_id: int = 2) -> str:
    return encode_jwt({"sub": str(patient_id), "patient_id": patient_id, "name": "测试患者"})


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_logout_revokes_current_token(client: TestClient, store: _RevocationStore):
    token = _token()
    assert client.get("/api/v1/auth/me", headers=_auth(token)).status_code == 200

    resp = client.post("/api/v1/auth/logout", headers=_auth(token), json={"all_devices": False})
    assert resp.status_code == 204
    assert len(store.revoked_jtis) == 1

    # 关键断言：token 未过期，但已被服务端撤销
    assert client.get("/api/v1/auth/me", headers=_auth(token)).status_code == 401


def test_logout_without_token_is_idempotent(client: TestClient):
    assert client.post("/api/v1/auth/logout").status_code == 204


def test_logout_with_invalid_token_is_idempotent(client: TestClient):
    assert client.post("/api/v1/auth/logout", headers=_auth("not-a-jwt")).status_code == 204


def test_logout_all_devices_invalidates_sibling_tokens(client: TestClient, store: _RevocationStore):
    mine = _token(2)
    others = _token(3)

    resp = client.post("/api/v1/auth/logout", headers=_auth(mine), json={"all_devices": True})
    assert resp.status_code == 204

    # 同账号的另一份 token（比如另一台设备）随之失效
    assert client.get("/api/v1/auth/me", headers=_auth(_token(2))).status_code == 401
    # 其他账号不受影响
    assert client.get("/api/v1/auth/me", headers=_auth(others)).status_code == 200
    assert ("patient", "2") in store.cutoffs


def test_patient_logout_all_does_not_touch_admin_scope(
    client: TestClient, store: _RevocationStore,
):
    """患者登出全部设备不应影响管理端 token（两端账号体系完全隔离）。"""
    client.post("/api/v1/auth/logout", headers=_auth(_token(7)), json={"all_devices": True})
    assert ("patient", "7") in store.cutoffs
    assert ("admin", "7") not in store.cutoffs


async def test_admin_logout_revokes_admin_token(store: _RevocationStore):
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(admin_auth_router.router)
    client = TestClient(app)

    token = encode_jwt({"sub": "7", "admin_id": 7, "username": "root", "role": "admin"})
    assert (await get_current_admin(token, x_tenant_code=None, db=None)).admin_id == "7"

    assert client.post("/api/v1/admin/auth/logout", headers=_auth(token)).status_code == 204
    with pytest.raises(BizException) as exc:
        await get_current_admin(token, x_tenant_code=None, db=None)
    assert exc.value.status_code == 401
