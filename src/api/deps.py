# src/api/deps.py
#
# 公共 FastAPI 依赖：JWT 鉴权。
#
# - get_current_user_id       ：强制登录（session 历史等必须本人数据的接口）
# - get_optional_user_id      ：允许匿名（聊天接口：未登录也能咨询，但无持久化）
#
# 两者的 user_id 都只从 Bearer Token 的 sub 解出，禁止信任请求体自报字段。
# 匿名用户在后端统一用 "anon" 作为身份前缀，永远不会与真实患者 ID（纯数字）冲突。
#
# 撤销校验：JWT 无状态，登出后必须由服务端记住"已作废"（Redis）。
# 这里每个请求都查一次撤销名单，代价是一次 Redis GET（带短超时与熔断）。

from __future__ import annotations

from fastapi import Depends
from loguru import logger

from src.core.config import get_settings
from src.core.exceptions import BizErrorCode, BizException
from src.utils import token_revocation as revocation
from src.utils.jwt_utils import (
    oauth2_scheme,
    oauth2_scheme_optional,
    token_issued_at,
    verify_jwt,
)

# OAuth2 bearer 规范要求 401 响应带 WWW-Authenticate 头
_UNAUTHORIZED_HEADERS = {"WWW-Authenticate": "Bearer"}

# 匿名身份占位符：Redis key / thread_id 中使用，绝不写入患者相关表
ANON_USER_ID = "anon"

# 管理员角色：只有带这些 role 的 token 才能操作管理端接口
ADMIN_ROLES = {"admin", "superadmin"}
ADMIN_ROLE = "admin"  # 兼容旧代码


def _unauthorized(message: str = "登录已失效，请重新登录") -> BizException:
    return BizException(
        BizErrorCode.UNAUTHORIZED, message,
        status_code=401, headers=_UNAUTHORIZED_HEADERS,
    )


async def assert_token_active(payload: dict, *, fail_closed: bool = False) -> None:
    """校验 token 是否已被登出撤销；已撤销抛 401。

    fail_closed=True 时，撤销存储不可用也视为失效（管理端可配置开启）。
    """
    jti = payload.get("jti")
    if jti:
        state = await revocation.is_jti_revoked(str(jti))
        if state is True:
            raise _unauthorized()
        if state is None and fail_closed:
            logger.warning("撤销存储不可用，管理端按 fail-closed 拒绝请求")
            raise _unauthorized("登录状态校验暂不可用，请稍后重试")

    subject = str(payload.get("sub") or "")
    issued_at = token_issued_at(payload)
    if subject and issued_at is not None:
        scope = ADMIN_ROLE if payload.get("role") == ADMIN_ROLE else "patient"
        state = await revocation.is_session_revoked(scope, subject, issued_at)
        if state is True:
            raise _unauthorized("该账号已退出登录，请重新登录")
        if state is None and fail_closed:
            logger.warning("撤销存储不可用，管理端按 fail-closed 拒绝请求")
            raise _unauthorized("登录状态校验暂不可用，请稍后重试")


async def _extract_payload(token: str, *, fail_closed: bool = False) -> dict:
    """校验 token 并返回完整 payload；失败抛 401（不区分过期/非法，避免泄露信息）。"""
    try:
        payload = verify_jwt(token)
    except Exception:
        raise _unauthorized() from None
    if not payload.get("sub"):
        logger.warning("JWT 缺少 sub 字段，拒绝访问")
        raise _unauthorized("无效令牌")
    await assert_token_active(payload, fail_closed=fail_closed)
    return payload


async def _extract_sub(token: str) -> str:
    """校验 token 并取出 sub；失败抛 401（不区分过期/非法，避免泄露信息）。

    管理员 token 一律拒绝：否则管理员 ID 会被当成 patients.id，
    聊天/会话/预约数据全部落到错误的档案上。
    """
    payload = await _extract_payload(token)
    if payload.get("role") == ADMIN_ROLE:
        raise BizException(BizErrorCode.FORBIDDEN, "管理员账号不能用于患者端接口", status_code=403)
    return str(payload["sub"])


async def get_current_user_id(token: str = Depends(oauth2_scheme)) -> str:
    """强制登录（患者端）：无 token / 过期 / 非法 / 已登出 一律 401。"""
    return await _extract_sub(token)


async def get_optional_user_id(token: str | None = Depends(oauth2_scheme_optional)) -> str | None:
    """可选登录：

    - 未带 Token        → None（游客模式，可聊天但不落库、不能新建会话）
    - 带了但校验不通过  → 401（提示前端重新登录，避免"假登录"状态下继续使用他人身份）
    - 校验通过          → JWT sub
    """
    if not token:
        return None
    return await _extract_sub(token)


class AdminIdentity:
    """管理端身份：管理端接口一律用它做守卫，避免只拿到裸 id 后误当患者 ID。"""

    def __init__(self, admin_id: str, username: str, role: str) -> None:
        self.admin_id = admin_id
        self.username = username
        self.role = role


async def get_current_admin(token: str = Depends(oauth2_scheme)) -> AdminIdentity:
    """管理端守卫：必须持有 role=admin 且未被登出的 token。

    - 未带 / 过期 / 非法 / 已登出 token → 401
    - 患者 token（无 role 或 role != admin）→ 403
    - 撤销存储不可用时按 AUTH_ADMIN_FAIL_CLOSED 决定放行或拒绝
    """
    fail_closed = bool(getattr(get_settings(), "AUTH_ADMIN_FAIL_CLOSED", False))
    payload = await _extract_payload(token, fail_closed=fail_closed)
    if payload.get("role") not in ADMIN_ROLES:
        logger.warning("非管理员 token 访问管理端接口，拒绝")
        raise BizException(BizErrorCode.FORBIDDEN, "需要管理员账号登录", status_code=403)
    return AdminIdentity(
        admin_id=str(payload["sub"]),
        username=str(payload.get("username") or ""),
        role=str(payload.get("role") or ADMIN_ROLE),
    )


async def get_current_admin_id(admin: AdminIdentity = Depends(get_current_admin)) -> str:
    """只需要管理员 ID 时的便捷依赖。"""
    return admin.admin_id
