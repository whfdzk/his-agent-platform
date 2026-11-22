"""add handoff_tickets

C3 人工接管第一步：工单表。

- handoff_tickets：患者申请转人工 / 模型自升级 → 一张工单，坐席认领后回复，
  回复写回 chat_messages，患者历史里能看到。

Revision ID: c8f4d1a6e329
Revises: b7e3c9f0d218
Create Date: 2026-11-20

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'c8f4d1a6e329'
down_revision: Union[str, Sequence[str], None] = 'b7e3c9f0d218'
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
        'handoff_tickets',
        *_base_columns(),
        sa.Column('ticket_no', sa.String(length=24), server_default='', nullable=False, comment='工单号'),
        sa.Column('patient_id', sa.BigInteger(), nullable=False, comment='申请人（patients.id）'),
        sa.Column('session_id', sa.String(length=100), nullable=False, comment='会话 ID'),
        sa.Column('conversation_id', sa.BigInteger(), nullable=True, comment='conversations.id'),
        sa.Column('status', sa.String(length=20), server_default='open', nullable=False, comment='open/claimed/resolved/closed/cancelled'),
        sa.Column('priority', sa.String(length=16), server_default='normal', nullable=False, comment='low/normal/high/urgent'),
        sa.Column('reason', sa.String(length=32), server_default='patient_request', nullable=False, comment='升级原因'),
        sa.Column('summary', sa.Text(), server_default='', nullable=False, comment='诉求摘要'),
        sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=True, comment='附加上下文'),
        sa.Column('assigned_to', sa.BigInteger(), nullable=True, comment='认领人 patients.id'),
        sa.Column('assigned_name', sa.String(length=64), server_default='', nullable=False, comment='认领人姓名'),
        sa.Column('claimed_at', sa.DateTime(), nullable=True, comment='认领时间'),
        sa.Column('resolved_at', sa.DateTime(), nullable=True, comment='解决时间'),
        sa.Column('resolution', sa.Text(), server_default='', nullable=False, comment='解决说明'),
        sa.Column('message_count', sa.Integer(), server_default='0', nullable=False, comment='坐席回复条数'),
        sa.Column('last_message_at', sa.DateTime(), nullable=True, comment='最后回复时间'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_handoff_ticket_status', 'handoff_tickets', ['status'])
    op.create_index('ix_handoff_ticket_patient', 'handoff_tickets', ['patient_id'])
    op.create_index('ix_handoff_ticket_session', 'handoff_tickets', ['patient_id', 'session_id'])
    op.create_index('iq_handoff_ticket_no', 'handoff_tickets', ['ticket_no'], unique=True)


def downgrade() -> None:
    op.drop_index('iq_handoff_ticket_no', table_name='handoff_tickets')
    op.drop_index('ix_handoff_ticket_session', table_name='handoff_tickets')
    op.drop_index('ix_handoff_ticket_patient', table_name='handoff_tickets')
    op.drop_index('ix_handoff_ticket_status', table_name='handoff_tickets')
    op.drop_table('handoff_tickets')
