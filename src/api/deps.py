# src/api/deps.py
#
# 公共 FastAPI 依赖：JWT 鉴权 + RBAC 守卫。
#
# 两层结构：
#   1) 身份解析：verify_jwt → payload → AuthContext（kind/roles/scopes 全部来自 token）
#   2) 权限守卫：require_auth / require_roles / require_scope 依赖工厂，按需挂到接口上
#
# - get_current_user_id  ：强制登录（患者端），返回 sub 字符串（保持既有返回契约）
# - get_optional_user_id ：允许匿名（聊天接口：未登录也能咨询，但无持久化）
# - get_current_admin    ：管理端守卫，返回 AdminIdentity
#
# 三者的 user_id 都只从 Bearer Token 的 sub 解出，禁止信任请求体自报字段。
# 匿名用户在后端统一用 "anon" 作为身份前缀，永远不会与真实患者 ID（纯数字）冲突。
#
# 撤销校验：JWT 无状态，登出后必须由服务端记住"已作废"（Redis）。
# 这里每个请求都查一次撤销名单，代价是一次 Redis GET（带短超时与熔断）。
#
# 兼容性说明（B1）：旧 token 的 payload 里没有 roles/scopes 字段，此处按 role 静态展开兜底，
# 因此升级后已登录用户不会立刻掉线；但要拿到精确的角色集（如医护的 patient:read）需重新登录。

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from fastapi import Depends, Header
from loguru import logger

from src.core.config import get_settings
from src.core.exceptions import BizErrorCode, BizException
from src.core.permissions import (
    KIND_ADMIN,
    KIND_PATIENT,
    WILDCARD,
    normalize_roles,
    scopes_for_roles,
)
from src.infra.database import get_db
from src.modules.tenant.model import DEFAULT_TENANT_ID
from src.modules.tenant.service import resolve_tenant
from src.utils import token_revocation as revocation
from src.utils.jwt_utils import (
    oauth2_scheme,
    oauth2_scheme_optional,
    token_issued_at,
    verify_jwt,
)
from sqlalchemy.ext.asyncio import AsyncSession

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


def _forbidden(message: str) -> BizException:
    return BizException(BizErrorCode.FORBIDDEN, message, status_code=403)


@dataclass(frozen=True)
class AuthContext:
    """解析后的请求身份。

    - kind   ：账号体系（patient / admin），决定"能不能访问该侧接口"
    - role   ：主角色，兼容旧代码 payload.get("role") 的判定语义
    - roles  ：完整角色集合（可多角色，如医生 + 管理员）
    - scopes ：已展开的权限集合；含 "*" 表示全权限
    """

    subject_id: str
    kind: str
    role: str
    roles: tuple[str, ...]
    scopes: frozenset[str]
    # C1：租户 id（tenants.id）。旧 token 没有 tid，按默认租户 1 兜底，
    # 保证升级瞬间已登录用户不掉线；重新登录后拿到真实 tid。
    tenant_id: int = DEFAULT_TENANT_ID
    username: str = ""
    payload: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def is_admin(self) -> bool:
        return self.kind == KIND_ADMIN

    def has_role(self, *roles: str) -> bool:
        if not roles:
            return True
        if WILDCARD in self.scopes:
            return True
        return any(str(r) in self.roles for r in roles)

    def has_scope(self, *scopes: str, require_all: bool = False) -> bool:
        """任一满足即可（默认）；require_all=True 时要求全部满足。"""
        if not scopes:
            return True
        if WILDCARD in self.scopes:
            return True
        if require_all:
            return all(str(s) in self.scopes for s in scopes)
        return any(str(s) in self.scopes for s in scopes)


@dataclass(frozen=True)
class TenantIdentity:
    """请求所属租户（C1）。

    code 是前端/调用方传入的租户标识（X-Tenant-Code），id 是 tickets 等表里的 tenant_id。
    """

    id: int
    code: str
    name: str


# 租户请求头：缺省时按默认租户处理（兼容老前端与既有脚本）
TENANT_HEADER = "X-Tenant-Code"


async def get_current_tenant(
    x_tenant_code: str | None = Header(default=None, alias=TENANT_HEADER),
    db: AsyncSession = Depends(get_db),
) -> TenantIdentity:
    """解析请求租户：缺省 → default；未知 → 404；停用 → 403。"""
    tenant = await resolve_tenant(db, x_tenant_code)
    return TenantIdentity(id=int(tenant.id), code=tenant.code, name=tenant.name)


def _tenant_id_from_payload(payload: dict) -> int:
    """token 里的 tid；缺失（旧 token）或非法时回落到默认租户。"""
    raw = payload.get("tid")
    try:
        tid = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_TENANT_ID
    return tid if tid > 0 else DEFAULT_TENANT_ID


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
        # superadmin 也必须记在管理端撤销域：此前只判 role == "admin"，
        # 导致 superadmin 的"登出全部设备"写到 patient 域，管理端那一边反而没被撤销。
        scope = ADMIN_ROLE if payload.get("role") in ADMIN_ROLES else "patient"
        state = await revocation.is_session_revoked(scope, subject, issued_at)
        if state is True:
            raise _unauthorized("该账号已退出登录，请重新登录")
        if state is None and fail_closed:
            logger.warning("撤销存储不可用，管理端按 fail-closed 拒绝请求")
            raise _unauthorized("登录状态校验暂不可用，请稍后重试")


def _context_from_payload(payload: dict) -> AuthContext:
    """payload → AuthContext。roles/scopes 缺失时按 role 静态展开（兼容旧 token）。"""
    role = str(payload.get("role") or "")
    kind = KIND_ADMIN if role in ADMIN_ROLES else KIND_PATIENT

    raw_roles = payload.get("roles")
    if not isinstance(raw_roles, (list, tuple)) or not raw_roles:
        raw_roles = [role] if role else []
    roles = normalize_roles(raw_roles, kind=kind)

    raw_scopes = payload.get("scopes")
    if isinstance(raw_scopes, (list, tuple)) and raw_scopes:
        scopes = frozenset(str(s) for s in raw_scopes if s)
    else:
        # 旧 token 没有 scopes：按角色静态展开，保证升级后不掉线
        scopes = scopes_for_roles(roles)

    return AuthContext(
        subject_id=str(payload.get("sub") or ""),
        kind=kind,
        role=role or (roles[0] if roles else ""),
        roles=roles,
        scopes=scopes,
        tenant_id=_tenant_id_from_payload(payload),
        username=str(payload.get("username") or payload.get("name") or ""),
        payload=payload,
    )


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


async def get_auth_context(token: str = Depends(oauth2_scheme)) -> AuthContext:
    """强制登录：返回完整身份上下文（含 roles/scopes）。"""
    return _context_from_payload(await _extract_payload(token))


async def get_optional_auth_context(
    token: str | None = Depends(oauth2_scheme_optional),
) -> AuthContext | None:
    """可选登录：未带 token 返回 None；带了但不合法一律 401（不静默降级为游客）。"""
    if not token:
        return None
    return _context_from_payload(await _extract_payload(token))


def require_auth(allow_admin: bool = False):
    """强制登录依赖；allow_admin=False 时管理员 token 被 403（患者端接口的默认态度）。"""

    async def _dep(ctx: AuthContext = Depends(get_auth_context)) -> AuthContext:
        if ctx.is_admin and not allow_admin:
            logger.warning("管理员账号访问患者端接口，拒绝")
            raise _forbidden("管理员账号不能用于患者端接口")
        return ctx

    return _dep


def require_roles(*roles: str):
    """要求持有任一给定角色。superadmin（scopes 含 *）始终通过。"""
    wanted = tuple(str(r) for r in roles)

    async def _dep(ctx: AuthContext = Depends(get_auth_context)) -> AuthContext:
        if not ctx.has_role(*wanted):
            logger.warning(
                f"角色不足：需要 {wanted}，当前 roles={list(ctx.roles)} kind={ctx.kind}"
            )
            raise _forbidden("当前账号无权访问该功能")
        return ctx

    return _dep


def require_scope(*scopes: str, require_all: bool = False):
    """要求持有任一给定权限（require_all=True 时要求全部）。"""
    wanted = tuple(str(s) for s in scopes)

    async def _dep(ctx: AuthContext = Depends(get_auth_context)) -> AuthContext:
        if not ctx.has_scope(*wanted, require_all=require_all):
            logger.warning(
                f"权限不足：需要 {wanted}，当前 scopes={sorted(ctx.scopes)} kind={ctx.kind}"
            )
            raise _forbidden("当前账号无权执行该操作")
        return ctx

    return _dep


def optional_require_scope(*scopes: str, require_all: bool = False):
    """允许匿名的权限守卫（用于"游客可用、登录后按角色收紧"的接口）。

    判定顺序：
      1) 无 token          → 放行（游客模式，业务层自己决定是否落库）
      2) 管理员 token      → 403（管理端账号不能借游客通道访问患者端接口）
      3) 有 token 但缺权限 → 403（已登录身份不允许降级为游客继续用）
    """
    wanted = tuple(str(s) for s in scopes)

    async def _dep(ctx: AuthContext | None = Depends(get_optional_auth_context)) -> AuthContext | None:
        if ctx is None:
            return None
        if ctx.is_admin:
            logger.warning("管理员账号通过游客通道访问患者端接口，拒绝")
            raise _forbidden("管理员账号不能用于患者端接口")
        if not ctx.has_scope(*wanted, require_all=require_all):
            logger.warning(
                f"权限不足（可选登录）：需要 {wanted}，当前 scopes={sorted(ctx.scopes)} roles={list(ctx.roles)}"
            )
            raise _forbidden("当前账号无权执行该操作")
        return ctx

    return _dep


async def _extract_sub(token: str) -> str:
    """校验 token 并取出 sub；失败抛 401（不区分过期/非法，避免泄露信息）。

    管理端 token 一律拒绝：否则管理员 ID 会被当成 patients.id，
    聊天/会话/预约数据全部落到错误的档案上。
    注意必须按 ADMIN_ROLES 整个集合判断 —— 只判 "admin" 会漏掉 superadmin，
    实测 superadmin 的 token 曾能直接读患者会话列表。
    """
    payload = await _extract_payload(token)
    if payload.get("role") in ADMIN_ROLES:
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

    def __init__(
        self, admin_id: str, username: str, role: str,
        roles: tuple[str, ...] = (), scopes: frozenset[str] = frozenset(),
        tenant_id: int = DEFAULT_TENANT_ID,
    ) -> None:
        self.admin_id = admin_id
        self.username = username
        self.role = role
        self.roles = roles or ((role,) if role else ())
        self.scopes = scopes
        # C1：管理员工作租户。admins 表无 tenant_id，token 里固定 tid=1；
        # 管理端切换租户时由 `X-Tenant-Code` 覆盖（见 get_current_admin）。
        self.tenant_id = tenant_id

    def has_scope(self, *scopes: str, require_all: bool = False) -> bool:
        if not scopes:
            return True
        if WILDCARD in self.scopes:
            return True
        if require_all:
            return all(str(s) in self.scopes for s in scopes)
        return any(str(s) in self.scopes for s in scopes)


async def get_current_admin(
    token: str = Depends(oauth2_scheme),
    x_tenant_code: str | None = Header(default=None, alias=TENANT_HEADER),
    db: AsyncSession = Depends(get_db),
) -> AdminIdentity:
    """管理端守卫：必须持有 role=admin 且未被登出的 token。

    - 未带 / 过期 / 非法 / 已登出 token → 401
    - 患者 token（无 role 或 role != admin）→ 403
    - 撤销存储不可用时按 AUTH_ADMIN_FAIL_CLOSED 决定放行或拒绝
    - 带 `X-Tenant-Code` 时按该租户工作（管理端租户切换）：未知 404、停用 403；
      不带则回落到 token 里的 `tid`（旧 token 无 tid 时为默认租户）。
    """
    fail_closed = bool(getattr(get_settings(), "AUTH_ADMIN_FAIL_CLOSED", False))
    payload = await _extract_payload(token, fail_closed=fail_closed)
    if payload.get("role") not in ADMIN_ROLES:
        logger.warning("非管理员 token 访问管理端接口，拒绝")
        raise BizException(BizErrorCode.FORBIDDEN, "需要管理员账号登录", status_code=403)
    ctx = _context_from_payload(payload)
    tenant_id = ctx.tenant_id
    code = (x_tenant_code or "").strip()
    if code:
        switched = int((await resolve_tenant(db, code)).id)
        # 跨租户切换只给超管：普通管理员只能在 token 自带租户内工作，
        # 否则任何管理员都能带一个 header 读到别家医院的数据。
        if switched != tenant_id and "superadmin" not in ctx.roles:
            raise _forbidden("仅超级管理员可切换租户")
        tenant_id = switched
    return AdminIdentity(
        admin_id=ctx.subject_id,
        username=ctx.username,
        role=ctx.role or ADMIN_ROLE,
        roles=ctx.roles,
        scopes=ctx.scopes,
        tenant_id=tenant_id,
    )


async def get_current_admin_id(admin: AdminIdentity = Depends(get_current_admin)) -> str:
    """只需要管理员 ID 时的便捷依赖。"""
    return admin.admin_id
