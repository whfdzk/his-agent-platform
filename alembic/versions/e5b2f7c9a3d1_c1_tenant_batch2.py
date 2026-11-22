"""C1 多租户第二批：26 张表加 tenant_id + 目录类唯一约束收窄到租户内

决策见 doc/2026-11-20-C1多租户改造设计.md 3.2：
- 档案与计划 5 张、医护 3 张、隐私与审计 7 张、会员 3 张、目录 2 张、医学知识 6 张
- 医学知识表（drugs/diseases/symptoms）默认全球共享，但加列留出"某医院维护自己的
  药品/疾病/症状目录"的余地；因此它们的 name 唯一约束要收窄成 (tenant_id, name)，
  否则第二家医院建同名科室/疾病会撞约束（隔离脚本 R5 就卡在这里）
- 所有表都保留 server_default='1' 兜底，存量数据自动归默认租户，无需 UPDATE

Revision ID: e5b2f7c9a3d1
Revises: d1a7c3e5b9f2
Create Date: 2026-11-21
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "e5b2f7c9a3d1"
down_revision: Union[str, Sequence[str], None] = "d1a7c3e5b9f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# 第二批 26 张表（按设计文档 3.2 分组）
BATCH2_TABLES = [
    # 档案与计划
    "patient_profiles",
    "health_plans",
    "health_reminders",
    "medication_records",
    "health_period_reports",
    # 医护
    "clinician_profiles",
    "clinician_departments",
    "consultations",
    # 隐私与审计
    "privacy_consents",
    "data_requests",
    "safety_events",
    "admin_audit_logs",
    "agent_traces",
    "agent_spans",
    "citations",
    # 会员
    "membership_plans",
    "membership_orders",
    "user_memberships",
    # 目录
    "departments",
    "hospitals",
    # 医学知识
    "drugs",
    "drug_details",
    "disease_drugs",
    "diseases",
    "disease_symptoms",
    "symptoms",
]

# 唯一约束重定：(表, 旧约束名, 旧列, 新约束名, 新列)
# 注意必须分开记旧列：降级时要先按"旧列"把约束恢复回去，再删 tenant_id 列。
# 若图省事复用新列（含 tenant_id），PG 删列时会连带把该约束一起删掉，降级后表就没唯一约束了。
UNIQUE_REWRITES = [
    ("departments", "departments_name_key", ["name"],
     "uq_departments_tenant_name", ["tenant_id", "name"]),
    ("diseases", "diseases_name_key", ["name"],
     "uq_diseases_tenant_name", ["tenant_id", "name"]),
    ("symptoms", "symptoms_name_key", ["name"],
     "uq_symptoms_tenant_name", ["tenant_id", "name"]),
    ("hospitals", "uq_hospitals_name", ["name"],
     "uq_hospitals_tenant_name", ["tenant_id", "name"]),
    ("drug_details", "drug_details_drug_id_key", ["drug_id"],
     "uq_drug_details_tenant_drug", ["tenant_id", "drug_id"]),
    ("patient_profiles", "uq_patient_profiles_patient", ["patient_id"],
     "uq_patient_profiles_tenant_patient", ["tenant_id", "patient_id"]),
    ("clinician_profiles", "uq_clinician_profiles_clinician", ["clinician_id"],
     "uq_clinician_profiles_tenant_clinician", ["tenant_id", "clinician_id"]),
    ("clinician_departments", "uq_clinician_departments", ["clinician_id", "department_id"],
     "uq_clinician_departments_tenant", ["tenant_id", "clinician_id", "department_id"]),
]


def upgrade() -> None:
    # ---------- 26 张表加 tenant_id + 单列索引 ----------
    for table in BATCH2_TABLES:
        op.add_column(
            table,
            sa.Column("tenant_id", sa.BigInteger(), server_default="1", nullable=False,
                      comment="租户 id（tenants.id），存量归 1"),
        )
        op.create_index(f"ix_{table}_tenant", table, ["tenant_id"])

    # ---------- 唯一约束收窄到租户内 ----------
    # 先建新约束再删旧的：同名/同列数据在默认租户内仍然唯一，
    # 这样即使中途失败也不会出现"没有唯一约束"的窗口
    for table, old_name, old_cols, new_name, new_cols in UNIQUE_REWRITES:
        op.create_unique_constraint(new_name, table, new_cols)
        op.drop_constraint(old_name, table, type_="unique")


def downgrade() -> None:
    for table, old_name, old_cols, new_name, new_cols in reversed(UNIQUE_REWRITES):
        op.create_unique_constraint(old_name, table, old_cols)
        op.drop_constraint(new_name, table, type_="unique")

    for table in reversed(BATCH2_TABLES):
        op.drop_index(f"ix_{table}_tenant", table_name=table)
        op.drop_column(table, "tenant_id")
