# src/api/routers/admin_tenants.py
#
# 管理端：租户管理（C1）。
# - 列表：看所有租户及其状态
# - 创建：新医院入驻，code 全局唯一（对外用它做 X-Tenant-Code）
# - 停用/启用：停用后该租户的患者登录与接口调用一律 403
#
# 租户不提供删除：物理删租户会让旗下 tenant_id 的数据全部变成孤儿，
# 停用足够；真要清数据走离线运维流程。

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_current_admin
from src.core.exceptions import BizErrorCode, BizException
from src.infra.database import get_db
from src.modules.tenant.model import TENANT_ACTIVE, TENANT_SUSPENDED, Tenant

router = APIRouter(
    prefix="/api/v1/admin/tenants",
    tags=["admin-tenants"],
    dependencies=[Depends(get_current_admin)],
)


def _serialize(t: Tenant) -> dict:
    return {
        "id": t.id,
        "code": t.code,
        "name": t.name,
        "status": t.status,
        "contact_name": t.contact_name,
        "contact_phone": t.contact_phone,
        "created_at": str(t.created_at) if t.created_at else None,
    }


class TenantCreate(BaseModel):
    code: str = Field(min_length=2, max_length=32, pattern=r"^[a-z0-9][a-z0-9\-_]*$")
    name: str = Field(min_length=1, max_length=100)
    contact_name: str | None = Field(default=None, max_length=50)
    contact_phone: str | None = Field(default=None, max_length=20)


@router.get("")
async def list_tenants(db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(Tenant).order_by(Tenant.id))).scalars().all()
    return {"tenants": [_serialize(t) for t in rows], "total": len(rows)}


@router.post("", status_code=201)
async def create_tenant(req: TenantCreate, db: AsyncSession = Depends(get_db)):
    """新建租户。code 重复直接 409，不做静默改名。"""
    exists = (
        await db.execute(select(Tenant).where(Tenant.code == req.code).limit(1))
    ).scalar_one_or_none()
    if exists is not None:
        raise BizException(BizErrorCode.CONFLICT, f"租户编码已存在：{req.code}", status_code=409)

    tenant = Tenant(
        code=req.code,
        name=req.name,
        status=TENANT_ACTIVE,
        contact_name=req.contact_name,
        contact_phone=req.contact_phone,
        settings={},
    )
    db.add(tenant)
    await db.flush()

    # 兼容两种读法：顶层 code / tenant.code（验收脚本与前端都不必猜结构）
    return {
        "message": "租户创建成功",
        "id": tenant.id,
        "code": tenant.code,
        "name": tenant.name,
        "tenant": _serialize(tenant),
    }


async def _set_status(db: AsyncSession, tenant_id: int, status: str) -> dict:
    tenant = await db.get(Tenant, int(tenant_id))
    if tenant is None:
        raise BizException(BizErrorCode.NOT_FOUND, "租户不存在", status_code=404)
    if tenant.code == "default" and status != TENANT_ACTIVE:
        # 默认租户承载全部存量数据，停用等于把现有用户全部踢下线
        raise BizException(BizErrorCode.CONFLICT, "默认租户不能停用", status_code=409)
    tenant.status = status
    await db.flush()
    return {"message": "已更新", "tenant": _serialize(tenant)}


@router.post("/{tenant_id}/suspend")
async def suspend_tenant(tenant_id: int, db: AsyncSession = Depends(get_db)):
    return await _set_status(db, tenant_id, TENANT_SUSPENDED)


@router.post("/{tenant_id}/activate")
async def activate_tenant(tenant_id: int, db: AsyncSession = Depends(get_db)):
    return await _set_status(db, tenant_id, TENANT_ACTIVE)
