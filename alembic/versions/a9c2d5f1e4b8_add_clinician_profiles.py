"""add clinician_profiles + appointments.doctor_clinician_id

医生档案（C4 续）：把预约里的 "doctor_name 自由文本" 变成能映射到医生账号的数据。

- clinician_profiles        ：职称 / 专长 / 工号 / 出诊状态（不参与授权判定）
- appointments.doctor_clinician_id：这条预约挂在哪个医生账号上，接诊确认后据此自动
  建立 clinician_patients(relation='consulted')，医生才真正"看到"自己的接诊患者。

为什么必须要 doctor_clinician_id：
  医生姓名是自由文本（"张伟 主任医师"），靠姓名反查账号在重名/改写时必然错配；
  错配的后果是把患者记录授权给了别的医生，属于越权，不能靠"大概对得上"兜住。

Revision ID: a9c2d5f1e4b8
Revises: f4c8e2a9731d
Create Date: 2026-11-20

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'a9c2d5f1e4b8'
down_revision: Union[str, Sequence[str], None] = 'f4c8e2a9731d'
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
        'clinician_profiles',
        *_base_columns(),
        sa.Column('clinician_id', sa.BigInteger(), nullable=False, comment='医护账号（patients.id）'),
        sa.Column('employee_no', sa.String(length=32), server_default='', nullable=False, comment='工号'),
        sa.Column('title', sa.String(length=64), server_default='', nullable=False,
                  comment='职称，如 主任医师 / 主治医师'),
        sa.Column('specialty', sa.String(length=128), server_default='', nullable=False,
                  comment='专长，如 冠心病介入'),
        sa.Column('intro', sa.String(length=255), server_default='', nullable=False,
                  comment='一句话简介（患者端选医生时展示）'),
        sa.Column('status', sa.String(length=20), server_default='active', nullable=False,
                  comment='出诊状态：active=在岗 / inactive=停诊'),
        sa.Column('note', sa.String(length=255), server_default='', nullable=False, comment='备注'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('clinician_id', name='uq_clinician_profiles_clinician'),
    )

    # 先加可空列再建索引：历史预约一条都不能动，doctor_clinician_id 允许为空
    op.add_column(
        'appointments',
        sa.Column('doctor_clinician_id', sa.BigInteger(), nullable=True,
                  comment='接诊医生账号（patients.id）；有值时接诊后自动建立 clinician_patients(consulted)'),
    )
    op.create_index('ix_appointments_doctor_clinician', 'appointments', ['doctor_clinician_id'])


def downgrade() -> None:
    op.drop_index('ix_appointments_doctor_clinician', table_name='appointments')
    op.drop_column('appointments', 'doctor_clinician_id')
    op.drop_table('clinician_profiles')
