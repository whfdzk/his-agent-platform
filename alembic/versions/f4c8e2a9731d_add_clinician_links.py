"""add clinician_departments / clinician_patients

C4（医护端）的授权基础：
- clinician_departments：医生属于哪些科室（本科室高危告警的可见范围）
- clinician_patients   ：哪些患者由该医生负责（全等级可读）

没有这两张表时，"clinician 角色"只能回答是不是医生，回答不了"能看哪些患者"，
按角色直接放行等于全院医生可翻任意患者记录。

Revision ID: f4c8e2a9731d
Revises: e1a7c3f82b90
Create Date: 2026-11-20

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'f4c8e2a9731d'
down_revision: Union[str, Sequence[str], None] = 'e1a7c3f82b90'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _base_columns() -> list[sa.Column]:
    return [
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
    ]


def upgrade() -> None:
    op.create_table(
        'clinician_departments',
        *_base_columns(),
        sa.Column('clinician_id', sa.BigInteger(), nullable=False, comment='医护账号（patients.id）'),
        sa.Column('department_id', sa.BigInteger(), nullable=False, comment='departments.id'),
        sa.Column('is_primary', sa.Boolean(), server_default=sa.text('false'), nullable=False,
                  comment='是否主科室'),
        sa.Column('granted_by', sa.String(length=64), server_default='', nullable=False,
                  comment='授予人，如 admin:1 / system:seed'),
        sa.Column('note', sa.String(length=255), server_default='', nullable=False, comment='备注'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('clinician_id', 'department_id', name='uq_clinician_departments'),
    )
    op.create_index('ix_clinician_departments_clinician', 'clinician_departments', ['clinician_id'])
    op.create_index('ix_clinician_departments_department', 'clinician_departments', ['department_id'])

    op.create_table(
        'clinician_patients',
        *_base_columns(),
        sa.Column('clinician_id', sa.BigInteger(), nullable=False, comment='医护账号（patients.id）'),
        sa.Column('patient_id', sa.BigInteger(), nullable=False, comment='patients.id'),
        sa.Column('relation', sa.String(length=24), server_default='assigned', nullable=False,
                  comment='关系来源：assigned / consulted'),
        sa.Column('department_id', sa.BigInteger(), nullable=True, comment='建立关系时所属科室'),
        sa.Column('granted_by', sa.String(length=64), server_default='', nullable=False, comment='建立人或来源'),
        sa.Column('note', sa.String(length=255), server_default='', nullable=False, comment='备注'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('clinician_id', 'patient_id', name='uq_clinician_patients'),
    )
    op.create_index('ix_clinician_patients_clinician', 'clinician_patients', ['clinician_id'])
    op.create_index('ix_clinician_patients_patient', 'clinician_patients', ['patient_id'])


def downgrade() -> None:
    op.drop_index('ix_clinician_patients_patient', table_name='clinician_patients')
    op.drop_index('ix_clinician_patients_clinician', table_name='clinician_patients')
    op.drop_table('clinician_patients')
    op.drop_index('ix_clinician_departments_department', table_name='clinician_departments')
    op.drop_index('ix_clinician_departments_clinician', table_name='clinician_departments')
    op.drop_table('clinician_departments')
