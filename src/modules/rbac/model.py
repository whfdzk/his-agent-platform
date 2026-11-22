# src/modules/rbac/model.py
#
# RBAC 数据表（B1 最小闭环，不含多租户）。
#
# 为什么不建统一 users 表：
#   patients / admins 已各自承载账号、密码、启用状态，且 consultations / conversations /
#   chat_messages 等表的外键都指向 patients.id。再抽一层 users 会牵动全部既有外键与
#   迁移，收益只有"表名统一"。这里用主体引用（subject_kind + subject_id）挂角色，
#   既能覆盖 patients(patient) 与 admins(admin)，新增医护账号时也不需要重建外键。
#
# 表之间的关系：
#   rbac_roles            —— 角色（code 为业务主键，如 patient/clinician/admin）
#   rbac_permissions      —— 权限码定义（资源:动作）
#   rbac_role_permissions —— 角色与权限的对应（种子由迁移写入，运行期判定走静态表）
#   rbac_subject_roles    —— 谁被授予了哪个角色（唯一约束防重复授予）
#
# 注：请求期的鉴权判定**不查这些表**，只看 JWT 内已展开的 roles/scopes，
# 见 src/core/permissions.py 的设计说明。

from sqlalchemy import BigInteger, Boolean, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from src.core.base_model import BaseModel


class RbacRole(BaseModel):
    __tablename__ = "rbac_roles"

    code: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="角色码：patient/clinician/nurse/admin/superadmin"
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False, comment="中文名称")
    side: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="所属账号体系：patient（患者端）/ admin（管理端）"
    )
    description: Mapped[str] = mapped_column(String(255), default="", comment="说明")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, comment="是否启用")

    __table_args__ = (Index("ux_rbac_roles_code", "code", unique=True),)


class RbacPermission(BaseModel):
    __tablename__ = "rbac_permissions"

    code: Mapped[str] = mapped_column(String(64), nullable=False, comment="权限码，形如 chat:read")
    name: Mapped[str] = mapped_column(String(64), nullable=False, comment="中文名称")
    description: Mapped[str] = mapped_column(String(255), default="", comment="说明")

    __table_args__ = (Index("ux_rbac_permissions_code", "code", unique=True),)


class RbacRolePermission(BaseModel):
    __tablename__ = "rbac_role_permissions"

    role_code: Mapped[str] = mapped_column(String(32), nullable=False, comment="角色码")
    permission_code: Mapped[str] = mapped_column(String(64), nullable=False, comment="权限码")

    __table_args__ = (
        UniqueConstraint("role_code", "permission_code", name="ux_rbac_role_permission"),
        Index("ix_rbac_role_permissions_role", "role_code"),
    )


class RbacSubjectRole(BaseModel):
    """主体（患者账号 / 管理员账号）与角色的授予关系。"""

    __tablename__ = "rbac_subject_roles"

    subject_kind: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="主体类型：patient / admin"
    )
    subject_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="主体 ID（patients.id / admins.id）")
    role_code: Mapped[str] = mapped_column(String(32), nullable=False, comment="角色码")
    granted_by: Mapped[str] = mapped_column(String(64), default="", comment="授予人标识（username 或 system）")
    note: Mapped[str] = mapped_column(String(255), default="", comment="授予备注")

    __table_args__ = (
        UniqueConstraint("subject_kind", "subject_id", "role_code", name="ux_rbac_subject_role"),
        Index("ix_rbac_subject_roles_subject", "subject_kind", "subject_id"),
    )
