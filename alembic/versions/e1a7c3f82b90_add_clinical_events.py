"""add clinical_events table

B3（急症分级落库）的库结构：
- clinical_events：记录 red / orange / yellow 三档急症判定（green 不落）。

此前的急症识别只产出回复文案，库里零记录，无法回答"识别过多少红旗症状、
分级是否合理、这些会话后来有没有真去急诊"。分级结果落库后，医护看板、
质控抽查与阈值调优才有数据基础。

Revision ID: e1a7c3f82b90
Revises: d5f2a8c71b36
Create Date: 2026-11-20

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'e1a7c3f82b90'
down_revision: Union[str, Sequence[str], None] = 'd5f2a8c71b36'
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
        'clinical_events',
        *_base_columns(),
        sa.Column('event_type', sa.String(length=32), server_default='emergency_triage', nullable=False,
                  comment='事件类型：emergency_triage 等'),
        sa.Column('level', sa.String(length=16), server_default='green', nullable=False,
                  comment='分级：red / orange / yellow / green'),
        sa.Column('reason', sa.String(length=255), server_default='', nullable=False,
                  comment='判定原因（模型短语，不含患者原话）'),
        sa.Column('symptoms', postgresql.JSONB(), nullable=True, comment='触发的症状词列表'),
        sa.Column('patient_id', sa.BigInteger(), nullable=True, comment='患者 ID（游客为空）'),
        sa.Column('user_id', sa.String(length=100), server_default='', nullable=False, comment='会话用户标识'),
        sa.Column('session_id', sa.String(length=100), server_default='', nullable=False, comment='会话 ID'),
        sa.Column('thread_id', sa.String(length=160), server_default='', nullable=False, comment='LangGraph thread_id'),
        sa.Column('trace_id', sa.String(length=64), server_default='', nullable=False, comment='关联 trace'),
        sa.Column('source', sa.String(length=24), server_default='agent', nullable=False,
                  comment='来源：agent / tool / clinician'),
        sa.Column('payload', postgresql.JSONB(), nullable=True, comment='附加信息（轮次、模型原始等级等）'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_clinical_events_patient', 'clinical_events', ['patient_id'])
    op.create_index('ix_clinical_events_level', 'clinical_events', ['level'])
    op.create_index('ix_clinical_events_session', 'clinical_events', ['session_id'])
    op.create_index('ix_clinical_events_type_level', 'clinical_events', ['event_type', 'level'])


def downgrade() -> None:
    op.drop_index('ix_clinical_events_type_level', table_name='clinical_events')
    op.drop_index('ix_clinical_events_session', table_name='clinical_events')
    op.drop_index('ix_clinical_events_level', table_name='clinical_events')
    op.drop_index('ix_clinical_events_patient', table_name='clinical_events')
    op.drop_table('clinical_events')
