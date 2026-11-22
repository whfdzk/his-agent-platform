# src/modules/tenant/service.py
#
# 租户解析（C1 第一步）：
#   请求头 X-Tenant-Code → 查 tenants → 拿到 tenant_id
#
# 解析策略：
#   1. 请求头缺省 → 默认租户 default（保证老前端、老脚本、Swagger 不改也能跑）
#   2. 请求头给了但库里没有 → 404（不要静默回落到默认租户，否则会把别人的数据写进东家）
#   3. 租户被停用 → 403
#
# 不做进程内缓存：登录/注册是低频接口，一次主键查询的成本可以忽略；
# 后续高频接口需要缓存时再加，并要有停用租户的失效路径。

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.exceptions import BizErrorCode, BizException
from src.modules.tenant.model import DEFAULT_TENANT_CODE, TENANT_ACTIVE, Tenant


async def resolve_tenant(db: AsyncSession, code: str | None) -> Tenant:
    """按 code 取租户；None / 空串按默认租户处理。"""
    wanted = (code or "").strip() or DEFAULT_TENANT_CODE
    tenant = (
        await db.execute(select(Tenant).where(Tenant.code == wanted).limit(1))
    ).scalar_one_or_none()

    if tenant is None:
        raise BizException(
            BizErrorCode.NOT_FOUND,
            f"租户不存在：{wanted}",
            status_code=404,
        )
    if tenant.status != TENANT_ACTIVE:
        raise BizException(
            BizErrorCode.FORBIDDEN,
            "该租户已停用，请联系管理员",
            status_code=403,
        )
    return tenant


async def resolve_tenant_id(db: AsyncSession, code: str | None) -> int:
    """只取 id 的便捷版本（登录查询、写库时用）。"""
    return int((await resolve_tenant(db, code)).id)


async def get_tenant(db: AsyncSession, tenant_id: int) -> Tenant | None:
    return await db.get(Tenant, int(tenant_id))
