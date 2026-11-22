"""add clinical_actions table

B2（高危动作草稿 + 确认）的库结构：
- clinical_actions：动作草稿与确认记录，状态机 draft → pending_confirm →
  approved / rejected / expired

此前"等患者确认预约"的状态只存在 Redis（ex=600），没有审计留痕、Redis 重启即丢、
管理端也看不到待确认清单。

Revision ID: d5f2a8c71b36
Revises: c3d7e9a15b84
Create Date: 2026-11-20

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'd5f2a8c71b36'
down_revision: Union[str, Sequence[str], None] = 'c3d7e9a15b84'
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
        'clinical_actions',
        *_base_columns(),
        sa.Column('action_type', sa.String(length=32), nullable=False,
                  comment='动作类型：appointment / handoff / medication / order'),
        sa.Column('status', sa.String(length=24), server_default='draft', nullable=False,
                  comment='状态：draft / pending_confirm / approved / rejected / expired'),
        sa.Column('patient_id', sa.BigInteger(), nullable=True, comment='患者 ID（游客或未识别时为空）'),
        sa.Column('user_id', sa.String(length=100), server_default='', nullable=False, comment='发起会话的用户标识'),
        sa.Column('session_id', sa.String(length=100), server_default='', nullable=False, comment='会话 ID'),
        sa.Column('thread_id', sa.String(length=160), server_default='', nullable=False, comment='LangGraph thread_id'),
        sa.Column('trace_id', sa.String(length=64), server_default='', nullable=False, comment='关联 trace'),
        sa.Column('source', sa.String(length=24), server_default='agent', nullable=False,
                  comment='发起来源：agent / tool / clinician'),
        sa.Column('requested_by', sa.String(length=64), server_default='', nullable=False,
                  comment='发起者标识，如 tool:appointment_manager'),
        sa.Column('summary', sa.String(length=255), server_default='', nullable=False,
                  comment='给患者看的一句话描述（不含 PHI）'),
        sa.Column('payload', postgresql.JSONB(), nullable=True, comment='拟执行动作的完整参数'),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True, comment='待确认超时时间'),
        sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True, comment='确认/拒绝时间'),
        sa.Column('decided_by', sa.String(length=64), server_default='', nullable=False,
                  comment='决策人：patient:<id> / clinician:<id> / system:timeout'),
        sa.Column('reject_reason', sa.String(length=255), server_default='', nullable=False, comment='拒绝原因'),
        sa.Column('result_ref', sa.String(length=64), server_default='', nullable=False,
                  comment='执行结果引用，如预约号 APTXXXX'),
        sa.Column('note', sa.Text(), server_default='', nullable=False, comment='备注'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_clinical_actions_patient', 'clinical_actions', ['patient_id'])
    op.create_index('ix_clinical_actions_status', 'clinical_actions', ['status'])
    op.create_index('ix_clinical_actions_session', 'clinical_actions', ['session_id'])
    op.create_index('ix_clinical_actions_status_expires', 'clinical_actions', ['status', 'expires_at'])


def downgrade() -> None:
    op.drop_index('ix_clinical_actions_status_expires', table_name='clinical_actions')
    op.drop_index('ix_clinical_actions_session', table_name='clinical_actions')
    op.drop_index('ix_clinical_actions_status', table_name='clinical_actions')
    op.drop_index('ix_clinical_actions_patient', table_name='clinical_actions')
    op.drop_table('clinical_actions')
