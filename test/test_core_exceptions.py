# 统一错误信封的确定性单测。
#
# 以前 API 层裸抛 HTTPException，前端只能按状态行分支；业务异常又统一返回
# 200 + {"code",...}，两种错误格式并存。现在全部走同一信封 + 正确的状态码。

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.core.exceptions import BizErrorCode, BizException, register_exception_handlers


@pytest.fixture
def client():
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/not-found")
    async def _not_found():
        raise BizException(BizErrorCode.NOT_FOUND, "资源不存在", status_code=404)

    @app.get("/legacy")
    async def _legacy():
        # 历史契约：老代码不传 status_code，仍按 200 返回，body 里带 code
        raise BizException(BizErrorCode.PARAM_ERROR, "业务异常")

    @app.get("/unhandled")
    async def _unhandled():
        raise RuntimeError("内部炸了")

    return TestClient(app, raise_server_exceptions=False)


def test_biz_exception_uses_envelope_with_real_status_code(client):
    resp = client.get("/not-found")
    assert resp.status_code == 404
    assert resp.json() == {"code": 40004, "message": "资源不存在", "data": None}


def test_legacy_biz_exception_keeps_200_contract(client):
    resp = client.get("/legacy")
    assert resp.status_code == 200
    assert resp.json()["code"] == 40001


def test_unhandled_exception_never_leaks_detail(client):
    resp = client.get("/unhandled")
    assert resp.status_code == 500
    body = resp.json()
    assert body["code"] == 50000
    assert "内部炸了" not in str(body)


def test_ww_authenticate_header_is_preserved():
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/secret")
    async def _secret():
        raise BizException(
            BizErrorCode.UNAUTHORIZED, "未登录",
            status_code=401, headers={"WWW-Authenticate": "Bearer"},
        )

    resp = TestClient(app, raise_server_exceptions=False).get("/secret")
    assert resp.status_code == 401
    assert resp.headers["WWW-Authenticate"] == "Bearer"


def test_error_codes_are_unique():
    codes = [int(code) for code in BizErrorCode]
    assert len(codes) == len(set(codes))


def test_biz_exception_defaults_to_business_code():
    exc = BizException()
    assert exc.code == 40001
    assert exc.status_code == 200
    assert str(exc) == "业务异常"
