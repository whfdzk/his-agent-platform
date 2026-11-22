# src/modules/tenant/model.py
#
# 租户表（C1）：医院级单层，tenant_id = 医院 id。
# 共享库 + 行级隔离：所有业务表通过 tenant_id 归属到某个租户，
# 不做 schema-per-tenant，运维成本与连接池规模都可控。

from __future__ import annotations

from sqlalchemy import Index, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.core.base_model import BaseModel

# 租户状态
TENANT_ACTIVE = "active"        # 正常服务
TENANT_SUSPENDED = "suspended"  # 停用：登录/接口一律拒绝

# 默认租户：存量数据全部归这里，header 缺省时也落到它
DEFAULT_TENANT_CODE = "default"
DEFAULT_TENANT_ID = 1


class Tenant(BaseModel):
    __tablename__ = "tenants"

    code: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="租户编码，如 default / hosp-01（对外用它做租户标识）"
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False, comment="医院名称")
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=TENANT_ACTIVE,
        server_default=TENANT_ACTIVE, comment="active / suspended",
    )
    contact_name: Mapped[str | None] = mapped_column(String(50), nullable=True, comment="联系人")
    contact_phone: Mapped[str | None] = mapped_column(String(20), nullable=True, comment="联系电话")
    settings: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"),
        comment="品牌/logo/默认科室等",
    )

    __table_args__ = (
        Index("uq_tenants_code", "code", unique=True),
        Index("ix_tenants_status", "status"),
    )
