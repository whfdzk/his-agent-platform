"""add medication / health plan / period report / hospital tables

补齐三个 Agent 的持久化能力：
- medication_records：MedicationAgent 的用户用药记录管理
- health_plans / health_period_reports：HealthManageAgent 的健康计划与周期报告
- hospitals：GuideAgent 的医院信息查询

Revision ID: c1d9e4b7a305
Revises: b4e8f2a61c93
Create Date: 2026-11-02

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'c1d9e4b7a305'
down_revision: Union[str, Sequence[str], None] = 'b4e8f2a61c93'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _base_columns() -> list[sa.Column]:
    return [
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
    ]


def upgrade() -> None:
    # 药品成分：过敏风险比对需要结构化字段，不能只靠说明书全文正则
    op.add_column(
        'drug_details',
        sa.Column('ingredient', sa.Text(), nullable=True, comment='主要成分'),
    )

    op.create_table(
        'medication_records',
        *_base_columns(),
        sa.Column('patient_id', sa.BigInteger(), nullable=False, comment='患者 ID'),
        sa.Column('drug_name', sa.String(length=100), nullable=False, comment='药品名称'),
        sa.Column('spec', sa.String(length=50), nullable=True, comment='规格'),
        sa.Column('dosage', sa.String(length=50), nullable=True, comment='单次剂量'),
        sa.Column('frequency', sa.String(length=50), nullable=True, comment='用药频次'),
        sa.Column('indication', sa.String(length=200), nullable=True, comment='用药目的'),
        sa.Column('start_date', sa.Date(), nullable=True, comment='开始日期'),
        sa.Column('end_date', sa.Date(), nullable=True, comment='结束日期'),
        sa.Column('status', sa.String(length=16), nullable=True, comment='active/stopped/finished'),
        sa.Column('source', sa.String(length=20), nullable=True, comment='manual/prescription/import'),
        sa.Column('note', sa.String(length=300), nullable=True, comment='备注'),
        sa.ForeignKeyConstraint(['patient_id'], ['patients.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_medication_records_patient_status', 'medication_records', ['patient_id', 'status'])
    op.create_index('ix_medication_records_patient_drug', 'medication_records', ['patient_id', 'drug_name'])

    op.create_table(
        'health_plans',
        *_base_columns(),
        sa.Column('patient_id', sa.BigInteger(), nullable=False, comment='患者 ID'),
        sa.Column('title', sa.String(length=100), nullable=False, comment='计划标题'),
        sa.Column('plan_type', sa.String(length=20), nullable=True, comment='diet/exercise/medication/monitor/other'),
        sa.Column('metric_type', sa.String(length=32), nullable=True, comment='关联指标类型'),
        sa.Column('target_text', sa.String(length=200), nullable=True, comment='目标描述'),
        sa.Column('frequency', sa.String(length=50), nullable=True, comment='执行频次'),
        sa.Column('start_date', sa.Date(), nullable=True, comment='开始日期'),
        sa.Column('end_date', sa.Date(), nullable=True, comment='结束日期'),
        sa.Column('status', sa.String(length=16), nullable=True, comment='active/completed/abandoned'),
        sa.Column('note', sa.String(length=300), nullable=True, comment='备注'),
        sa.ForeignKeyConstraint(['patient_id'], ['patients.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_health_plans_patient_status', 'health_plans', ['patient_id', 'status'])

    op.create_table(
        'health_period_reports',
        *_base_columns(),
        sa.Column('patient_id', sa.BigInteger(), nullable=False, comment='患者 ID'),
        sa.Column('period_type', sa.String(length=16), nullable=True, comment='week/month'),
        sa.Column('period_start', sa.Date(), nullable=False, comment='周期起始日'),
        sa.Column('period_end', sa.Date(), nullable=False, comment='周期结束日'),
        sa.Column('summary', sa.Text(), nullable=True, comment='一句话摘要'),
        sa.Column('content', sa.Text(), nullable=True, comment='报告正文'),
        sa.Column('stats', postgresql.JSONB(), nullable=True, comment='统计快照'),
        sa.ForeignKeyConstraint(['patient_id'], ['patients.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_health_period_reports_patient', 'health_period_reports', ['patient_id', 'period_end'])

    op.create_table(
        'hospitals',
        *_base_columns(),
        sa.Column('name', sa.String(length=100), nullable=False, comment='医院名称'),
        sa.Column('level', sa.String(length=20), nullable=True, comment='等级'),
        sa.Column('hospital_type', sa.String(length=20), nullable=True, comment='类型'),
        sa.Column('province', sa.String(length=30), nullable=True, comment='省份'),
        sa.Column('city', sa.String(length=30), nullable=True, comment='城市'),
        sa.Column('address', sa.String(length=200), nullable=True, comment='地址'),
        sa.Column('phone', sa.String(length=30), nullable=True, comment='咨询电话'),
        sa.Column('emergency_phone', sa.String(length=30), nullable=True, comment='急诊电话'),
        sa.Column('open_hours', sa.String(length=100), nullable=True, comment='门诊时间'),
        sa.Column('intro', sa.Text(), nullable=True, comment='医院简介'),
        sa.Column('departments', sa.Text(), nullable=True, comment='重点科室'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name', name='uq_hospitals_name'),
    )
    op.create_index('ix_hospitals_city', 'hospitals', ['city'])


def downgrade() -> None:
    op.drop_column('drug_details', 'ingredient')
    op.drop_index('ix_hospitals_city', table_name='hospitals')
    op.drop_table('hospitals')
    op.drop_index('ix_health_period_reports_patient', table_name='health_period_reports')
    op.drop_table('health_period_reports')
    op.drop_index('ix_health_plans_patient_status', table_name='health_plans')
    op.drop_table('health_plans')
    op.drop_index('ix_medication_records_patient_drug', table_name='medication_records')
    op.drop_index('ix_medication_records_patient_status', table_name='medication_records')
    op.drop_table('medication_records')
