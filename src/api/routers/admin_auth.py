# src/api/routers/admin_auth.py
#
# 管理端登录 API（与患者端 /api/v1/auth 完全隔离）：
# - POST /api/v1/admin/auth/login：用户名 + 密码 → 带 role=admin 的 JWT
# - GET    /api/v1/admin/auth/me ：校验当前管理员 token，供前端刷新登录态
#
# 建号不走接口：管理端账号由 scripts/create_admin.py 离线创建，
# 避免暴露一个谁都能注册管理员的端点。

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, Header, Request, Response, status
from loguru import logger
from pydantic import BaseModel, Field

from src.api.deps import ADMIN_ROLE, get_current_admin
from src.api.rate_limit import enforce_login_rate
from src.core.exceptions import BizErrorCode, BizException
from src.infra.database import get_db
from src.modules.admin.model import Admin
from src.utils import token_revocation as revocation
from src.utils.jwt_utils import encode_jwt, token_remaining_seconds, verify_jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/api/v1/admin/auth", tags=["admin-auth"])


def _serialize(a: Admin) -> dict:
    return {
        "id": a.id,
        "username": a.username,
        "display_name": a.display_name or a.username,
        "role": a.role or ADMIN_ROLE,
    }


def issue_admin_token(a: Admin) -> str:
    """签发管理员 token。role 是管理端守卫的唯一依据，必须写进 payload。"""
    return encode_jwt({
        "sub": str(a.id),
        "admin_id": a.id,
        "username": a.username,
        "role": a.role or ADMIN_ROLE,
    })


class AdminLoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=64)


class AdminLoginResponse(BaseModel):
    message: str
    token: str
    admin: dict


@router.post("/login", response_model=AdminLoginResponse)
async def admin_login(
    req: AdminLoginRequest, request: Request, db: AsyncSession = Depends(get_db),
):
    # 管理端账号权限大，限流比患者端更严格（同样 5 次/5 分钟，账号 + IP 双维度）
    await enforce_login_rate(req.username, request.client.host if request.client else "")
    admin = (
        await db.execute(select(Admin).where(Admin.username == req.username).limit(1))
    ).scalar_one_or_none()

    # 账号不存在与密码错误统一文案：不暴露账号是否注册过
    if admin is None or not admin.enabled:
        logger.info(f"管理端登录失败（账号不存在或已停用）: {req.username}")
        raise BizException(BizErrorCode.UNAUTHORIZED, "账号或密码错误", status_code=401)

    from src.utils.password_utils import verify_password

    if not verify_password(req.password, admin.password_hash):
        logger.info(f"管理端登录失败（密码错误）: {req.username}")
        raise BizException(BizErrorCode.UNAUTHORIZED, "账号或密码错误", status_code=401)

    return {"message": "登录成功", "token": issue_admin_token(admin), "admin": _serialize(admin)}


class AdminLogoutRequest(BaseModel):
    """登出参数：all_devices=true 时同时让该管理员其它设备上的 token 失效。"""

    all_devices: bool = False


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def admin_logout(
    req: AdminLogoutRequest | None = None,
    authorization: str | None = Header(default=None, description="Bearer <token>"),
):
    """管理端登出：把当前 token 加入撤销名单，可选同时失效该账号所有 token。

    幂等：token 缺失 / 非法 / 已过期一律 204。管理端 token 权限更大，
    因此默认就带撤销能力，不能只依赖前端丢弃 localStorage。
    """
    payload = None
    if authorization and authorization.startswith("Bearer "):
        token = authorization.removeprefix("Bearer ").strip()
        if token:
            try:
                payload = verify_jwt(token)
            except Exception as e:
                logger.info(f"管理端登出时 token 已失效，按已登出处理: {type(e).__name__}")

    if payload is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    admin_id = str(payload.get("sub") or "")
    jti = payload.get("jti")
    if jti:
        await revocation.revoke_jti(str(jti), token_remaining_seconds(payload))

    all_devices = bool(req and req.all_devices)
    if all_devices and admin_id:
        await revocation.revoke_session_before(ADMIN_ROLE, admin_id, time.time())

    logger.info(f"管理端登出 admin_id={admin_id or '-'} all_devices={all_devices}")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me")
async def admin_me(admin=Depends(get_current_admin)):
    """返回当前管理员身份；token 失效/非管理员时由依赖直接 401/403。"""
    return {
        "admin": {
            "id": admin.admin_id,
            "username": admin.username,
            "role": admin.role,
        }
    }
