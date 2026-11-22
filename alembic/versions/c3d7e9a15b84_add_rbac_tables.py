"""add rbac tables and seed roles

B1（RBAC 最小闭环，不含多租户）的库结构：
- rbac_roles            ：角色定义（patient/clinician/nurse/admin/superadmin）
- rbac_permissions      ：权限码定义（资源:动作）
- rbac_role_permissions ：角色 → 权限
- rbac_subject_roles    ：账号 → 角色（subject_kind 区分 patients/admins）

不建统一 users 表的理由见 src/modules/rbac/model.py 顶部说明。
种子数据从 src/core/permissions.py 读取，保证迁移与请求期静态表一致；启动时
service.seed_defaults 还会幂等补齐，因此两边不会漂移。

Revision ID: c3d7e9a15b84
Revises: b6d1f8c3a942
Create Date: 2026-11-20

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from src.core.permissions import KIND_ADMIN, KIND_PATIENT, PERMISSION_DEFS, ROLE_DEFS

# revision identifiers, used by Alembic.
revision: str = 'c3d7e9a15b84'
down_revision: Union[str, Sequence[str], None] = 'b6d1f8c3a942'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _base_columns() -> list[sa.Column]:
    return [
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
    ]


def upgrade() -> None:
    # ── 角色 ────────────────────────────────────────────────────────────
    op.create_table(
        'rbac_roles',
        *_base_columns(),
        sa.Column('code', sa.String(length=32), nullable=False, comment='角色码：patient/clinician/nurse/admin/superadmin'),
        sa.Column('name', sa.String(length=64), nullable=False, comment='中文名称'),
        sa.Column('side', sa.String(length=16), nullable=False, comment='所属账号体系：patient/admin'),
        sa.Column('description', sa.String(length=255), server_default='', nullable=False, comment='说明'),
        sa.Column('enabled', sa.Boolean(), server_default=sa.text('true'), nullable=False, comment='是否启用'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ux_rbac_roles_code', 'rbac_roles', ['code'], unique=True)

    # ── 权限 ────────────────────────────────────────────────────────────
    op.create_table(
        'rbac_permissions',
        *_base_columns(),
        sa.Column('code', sa.String(length=64), nullable=False, comment='权限码，形如 chat:read'),
        sa.Column('name', sa.String(length=64), nullable=False, comment='中文名称'),
        sa.Column('description', sa.String(length=255), server_default='', nullable=False, comment='说明'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ux_rbac_permissions_code', 'rbac_permissions', ['code'], unique=True)

    # ── 角色 → 权限 ─────────────────────────────────────────────────────
    op.create_table(
        'rbac_role_permissions',
        *_base_columns(),
        sa.Column('role_code', sa.String(length=32), nullable=False, comment='角色码'),
        sa.Column('permission_code', sa.String(length=64), nullable=False, comment='权限码'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('role_code', 'permission_code', name='ux_rbac_role_permission'),
    )
    op.create_index('ix_rbac_role_permissions_role', 'rbac_role_permissions', ['role_code'])

    # ── 账号 → 角色 ─────────────────────────────────────────────────────
    op.create_table(
        'rbac_subject_roles',
        *_base_columns(),
        sa.Column('subject_kind', sa.String(length=16), nullable=False, comment='主体类型：patient/admin'),
        sa.Column('subject_id', sa.BigInteger(), nullable=False, comment='主体 ID（patients.id / admins.id）'),
        sa.Column('role_code', sa.String(length=32), nullable=False, comment='角色码'),
        sa.Column('granted_by', sa.String(length=64), server_default='', nullable=False, comment='授予人标识'),
        sa.Column('note', sa.String(length=255), server_default='', nullable=False, comment='授予备注'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('subject_kind', 'subject_id', 'role_code', name='ux_rbac_subject_role'),
    )
    op.create_index('ix_rbac_subject_roles_subject', 'rbac_subject_roles', ['subject_kind', 'subject_id'])

    # ── 种子：角色 / 权限 / 映射 ────────────────────────────────────────
    roles_table = sa.table(
        'rbac_roles',
        sa.column('code', sa.String), sa.column('name', sa.String),
        sa.column('side', sa.String), sa.column('description', sa.String),
        sa.column('enabled', sa.Boolean),
    )
    op.bulk_insert(roles_table, [
        {
            "code": code, "name": spec["name"], "side": spec["side"],
            "description": spec["description"], "enabled": True,
        }
        for code, spec in ROLE_DEFS.items()
    ])

    perms_table = sa.table(
        'rbac_permissions',
        sa.column('code', sa.String), sa.column('name', sa.String), sa.column('description', sa.String),
    )
    op.bulk_insert(perms_table, [
        {"code": code, "name": desc, "description": desc}
        for code, desc in PERMISSION_DEFS.items()
    ])

    mappings_table = sa.table(
        'rbac_role_permissions',
        sa.column('role_code', sa.String), sa.column('permission_code', sa.String),
    )
    rows: list[dict] = []
    for role, spec in ROLE_DEFS.items():
        for perm in spec["scopes"]:
            if perm == "*":
                continue  # 通配不落映射行：superadmin 的 scopes 由静态表展开
            rows.append({"role_code": role, "permission_code": perm})
    op.bulk_insert(mappings_table, rows)

    # ── 存量账号补授角色 ───────────────────────────────────────────────
    # 管理员：role 字段为 superadmin 的授 superadmin，其余授 admin
    op.execute(sa.text(f"""
        INSERT INTO rbac_subject_roles
            (subject_kind, subject_id, role_code, granted_by, note, created_at, updated_at)
        SELECT '{KIND_ADMIN}', a.id,
               CASE WHEN lower(coalesce(a.role, '')) = 'superadmin' THEN 'superadmin' ELSE 'admin' END,
               'migration', '存量账号补授', now(), now()
        FROM admins a
        WHERE a.id IS NOT NULL
        ON CONFLICT (subject_kind, subject_id, role_code) DO NOTHING
    """))

    # 患者：只给已设密码（真正会登录）的账号授予 patient，历史空档案不给
    op.execute(sa.text(f"""
        INSERT INTO rbac_subject_roles
            (subject_kind, subject_id, role_code, granted_by, note, created_at, updated_at)
        SELECT '{KIND_PATIENT}', p.id, 'patient', 'migration', '存量账号补授', now(), now()
        FROM patients p
        WHERE p.password_hash IS NOT NULL AND length(p.password_hash) > 0
        ON CONFLICT (subject_kind, subject_id, role_code) DO NOTHING
    """))


def downgrade() -> None:
    op.drop_index('ix_rbac_subject_roles_subject', table_name='rbac_subject_roles')
    op.drop_table('rbac_subject_roles')
    op.drop_index('ix_rbac_role_permissions_role', table_name='rbac_role_permissions')
    op.drop_table('rbac_role_permissions')
    op.drop_index('ux_rbac_permissions_code', table_name='rbac_permissions')
    op.drop_table('rbac_permissions')
    op.drop_index('ux_rbac_roles_code', table_name='rbac_roles')
    op.drop_table('rbac_roles')
