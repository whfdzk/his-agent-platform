# 患者端 /auth/me 鉴权的确定性单测。
#
# 回归点：形参曾写成裸 str，FastAPI 会当成 query 参数，
# Authorization 头永远读不到 → 登录用户每次刷新资料都返回 401。

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.routers import auth as auth_router
from src.core.exceptions import register_exception_handlers
from src.utils.jwt_utils import encode_jwt


class _FakeDB:
    """只实现 me() 用到的 db.get。"""

    async def get(self, _model, pk):
        return SimpleNamespace(
            id=pk, name="测试患者", phone="13900001111", gender="男", age=30
        )


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    # 复用生产异常处理器，BizException 才会转成 401 响应而不是抛出
    register_exception_handlers(app)
    app.include_router(auth_router.router)
    app.dependency_overrides[auth_router.get_db] = lambda: _FakeDB()
    return TestClient(app)


def _token() -> str:
    return encode_jwt({"sub": "2", "patient_id": 2, "name": "测试患者"})


def test_me_reads_authorization_header(client: TestClient):
    resp = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {_token()}"})
    assert resp.status_code == 200
    assert resp.json()["user"]["id"] == 2


def test_me_ignores_query_parameter(client: TestClient):
    """Authorization 只认请求头；query 传参不能被当成凭证。"""
    resp = client.get("/api/v1/auth/me", params={"authorization": f"Bearer {_token()}"})
    assert resp.status_code == 401


def test_me_rejects_missing_header(client: TestClient):
    assert client.get("/api/v1/auth/me").status_code == 401
