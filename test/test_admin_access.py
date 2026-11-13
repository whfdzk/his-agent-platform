# 管理端鉴权的确定性单测。
#
# 关键回归点：管理端接口此前完全裸奔（无任何鉴权），且患者 token 与管理端
# 是同一类 token —— 只要患者拿自己的 token 打管理端接口就能改平台配置。
#
# 鉴权依赖现在是 async（要查 Redis 撤销名单），因此用例改为 await 调用；
# Redis 层由 test/conftest.py 的 autouse fixture 替身，不依赖外部服务。

import pytest

from src.api.deps import ADMIN_ROLE, AdminIdentity, get_current_admin, get_current_user_id
from src.core.exceptions import BizException
from src.utils.jwt_utils import encode_jwt, verify_jwt


def _admin_token(sub: str = "7", role: str = ADMIN_ROLE) -> str:
    return encode_jwt({"sub": sub, "admin_id": int(sub), "username": "root", "role": role})


def _patient_token(sub: str = "1001") -> str:
    # 患者 token 与线上一致：只有 sub / patient_id，没有 role
    return encode_jwt({"sub": sub, "patient_id": int(sub), "name": "张三"})


def test_admin_token_carries_role():
    assert verify_jwt(_admin_token())["role"] == ADMIN_ROLE


def test_tokens_carry_jti_and_short_ttl():
    """登出需要 jti；有效期必须是分钟级，否则登出后的残留窗口太长。"""
    payload = verify_jwt(_admin_token())
    assert payload["jti"]
    assert payload["typ"] == "access"
    # 默认 30 分钟（留一点余量给执行耗时）
    assert payload["exp"] - payload["iat"] <= 35 * 60


async def test_get_current_admin_accepts_admin_token():
    admin = await get_current_admin(_admin_token("7"))
    assert isinstance(admin, AdminIdentity)
    assert admin.admin_id == "7"
    assert admin.username == "root"
    assert admin.role == ADMIN_ROLE


async def test_get_current_admin_rejects_patient_token():
    """患者 token 打管理端接口必须 403，不能只因为"登录了"就放行。"""
    with pytest.raises(BizException) as exc:
        await get_current_admin(_patient_token())
    assert exc.value.status_code == 403


async def test_get_current_admin_rejects_unknown_role():
    with pytest.raises(BizException) as exc:
        await get_current_admin(_admin_token(role="viewer"))
    assert exc.value.status_code == 403


async def test_get_current_admin_rejects_invalid_token():
    with pytest.raises(BizException) as exc:
        await get_current_admin("not-a-jwt")
    assert exc.value.status_code == 401


async def test_patient_endpoints_reject_admin_token():
    """反向也要堵：管理员 ID 不是 patients.id，落库会写到错误档案上。"""
    with pytest.raises(BizException) as exc:
        await get_current_user_id(_admin_token())
    assert exc.value.status_code == 403


async def test_patient_endpoints_still_accept_patient_token():
    assert await get_current_user_id(_patient_token("1001")) == "1001"
