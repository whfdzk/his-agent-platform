"""C1 多租户第一批：tenants 表 + 14 张核心业务表加 tenant_id

决策见 doc/2026-11-20-C1多租户改造设计.md：
- 医院级单层，tenant_id = 医院 id
- 共享库 + 行级隔离，不引入 schema-per-tenant
- 存量数据整体归默认租户 1（列默认值直接完成回填，无需 UPDATE）

本迁移只做第一批（14 张核心表），第二批 26 张表 + departments/hospitals
唯一约束重定放在下一个 revision，避免一次 DDL 动 40 张表时无法定位失败点。

注意：tenant_id 保留 server_default='1' 作兜底，但应用层必须显式写入，
否则新租户的数据会静默落到默认租户。

Revision ID: d1a7c3e5b9f2
Revises: c8f4d1a6e329
Create Date: 2026-11-20
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "d1a7c3e5b9f2"
down_revision: Union[str, Sequence[str], None] = "c8f4d1a6e329"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# 第一批：与业务同批上线的 14 张核心表
CORE_TABLES = [
    "patients",
    "conversations",
    "chat_messages",
    "appointments",
    "clinical_actions",
    "clinical_events",
    "handoff_tickets",
    "health_reports",
    "report_metrics",
    "vital_metrics",
    "clinician_patients",
    "knowledge_feedback",
    "knowledge_notifications",
    "knowledge_audit_logs",
]

# 热表复合索引：(索引名, 表, 列)
COMPOSITE_INDEXES = [
    ("ix_patients_tenant_phone", "patients", ["tenant_id", "phone"]),
    ("ix_patients_tenant_name", "patients", ["tenant_id", "name"]),
    ("ix_conversations_tenant_user_updated", "conversations", ["tenant_id", "user_id", "updated_at"]),
    ("ix_handoff_tickets_tenant_status", "handoff_tickets", ["tenant_id", "status"]),
    ("ix_vital_metrics_tenant_patient_measured", "vital_metrics", ["tenant_id", "patient_id", "measured_at"]),
    # appointments 的日期列名是 date（varchar），不是 appointment_date
    ("ix_appointments_tenant_doctor_date", "appointments", ["tenant_id", "doctor_name", "date"]),
]


def upgrade() -> None:
    # ---------- tenants ----------
    op.create_table(
        "tenants",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False, comment="租户编码，如 default / hosp-01"),
        sa.Column("name", sa.String(length=100), nullable=False, comment="医院名称"),
        sa.Column("status", sa.String(length=16), server_default="active", nullable=False,
                  comment="active / suspended"),
        sa.Column("contact_name", sa.String(length=50), nullable=True),
        sa.Column("contact_phone", sa.String(length=20), nullable=True),
        sa.Column("settings", postgresql.JSONB(astext_type=sa.Text()),
                  server_default=sa.text("'{}'::jsonb"), nullable=False,
                  comment="品牌/logo/默认科室等"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("uq_tenants_code", "tenants", ["code"], unique=True)
    op.create_index("ix_tenants_status", "tenants", ["status"])

    # 默认租户：存量数据全部归到这里
    op.execute(
        "insert into tenants (id, code, name, status, settings, created_at, updated_at) "
        "values (1, 'default', '默认租户', 'active', '{}', now(), now())"
    )
    # 显式 insert 了 id=1，序列要推到 1，否则下一个新租户会撞主键
    op.execute("select setval('tenants_id_seq', 1, true)")

    # ---------- 14 张核心表加 tenant_id ----------
    for table in CORE_TABLES:
        op.add_column(
            table,
            sa.Column("tenant_id", sa.BigInteger(), server_default="1", nullable=False,
                      comment="租户 id（tenants.id），存量归 1"),
        )
        op.create_index(f"ix_{table}_tenant", table, ["tenant_id"])

    # ---------- 热表复合索引 ----------
    for name, table, cols in COMPOSITE_INDEXES:
        op.create_index(name, table, cols)


def downgrade() -> None:
    for name, table, _cols in reversed(COMPOSITE_INDEXES):
        op.drop_index(name, table_name=table)

    for table in reversed(CORE_TABLES):
        op.drop_index(f"ix_{table}_tenant", table_name=table)
        op.drop_column(table, "tenant_id")

    op.drop_index("ix_tenants_status", table_name="tenants")
    op.drop_index("uq_tenants_code", table_name="tenants")
    op.drop_table("tenants")
