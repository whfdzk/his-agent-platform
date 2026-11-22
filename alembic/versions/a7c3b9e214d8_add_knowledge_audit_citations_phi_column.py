"""add knowledge audit, citations and phi redacted column

档 A（安全可信闭环）三件事的库结构：
- knowledge_audit_logs：知识检索审计落库（此前只写 loguru，日志轮转即丢失）
- citations：回答引用结构化（文档名/页码/片段序号/分数/摘句）
- chat_messages.content_redacted：PHI 双列（原文留档 + 脱敏副本对外）

Revision ID: a7c3b9e214d8
Revises: d8a2f5c9e317
Create Date: 2026-11-20

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'a7c3b9e214d8'
down_revision: Union[str, Sequence[str], None] = 'd8a2f5c9e317'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _base_columns() -> list[sa.Column]:
    return [
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
    ]


def upgrade() -> None:
    # ── PHI 双列：原文列不动，新增脱敏副本列 ─────────────────────────────
    # 已有行为空串：历史消息没有脱敏副本，后台读取时回落到原文（见 admin_conversations）
    op.add_column(
        'chat_messages',
        sa.Column(
            'content_redacted', sa.Text(), server_default='', nullable=False,
            comment='PHI 脱敏后的消息文本（对外展示/导出/统计使用）',
        ),
    )

    # ── 知识检索审计 ────────────────────────────────────────────────────
    op.create_table(
        'knowledge_audit_logs',
        *_base_columns(),
        sa.Column('trace_id', sa.String(length=64), server_default='', nullable=False, comment='关联的 trace_id'),
        sa.Column('request_id', sa.String(length=64), server_default='', nullable=False, comment='请求 ID'),
        sa.Column('user_id', sa.String(length=100), server_default='', nullable=False, comment='提问用户'),
        sa.Column('role', sa.String(length=20), server_default='patient', nullable=False, comment='提问角色'),
        sa.Column('intent', sa.String(length=32), server_default='', nullable=False, comment='检索意图'),
        sa.Column('channels', postgresql.JSONB(), nullable=True, comment='实际使用的检索通道列表'),
        sa.Column('question', sa.Text(), server_default='', nullable=False, comment='提问（脱敏截断 200 字）'),
        sa.Column('answer_preview', sa.Text(), server_default='', nullable=False, comment='回答预览（脱敏截断 200 字）'),
        sa.Column('duration_ms', sa.Float(), server_default='0', nullable=False, comment='检索+生成耗时（毫秒）'),
        sa.Column('grounded', sa.Boolean(), nullable=True, comment='引用核查结论：True=有文档支撑'),
        sa.Column('hit_count', sa.Integer(), server_default='0', nullable=False, comment='命中片段数'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_knowledge_audit_user', 'knowledge_audit_logs', ['user_id'])
    op.create_index('ix_knowledge_audit_intent', 'knowledge_audit_logs', ['intent'])
    op.create_index('ix_knowledge_audit_trace', 'knowledge_audit_logs', ['trace_id'])

    # ── 回答引用 ────────────────────────────────────────────────────────
    op.create_table(
        'citations',
        *_base_columns(),
        sa.Column('trace_id', sa.String(length=64), server_default='', nullable=False, comment='关联 trace_id'),
        sa.Column('turn_id', sa.String(length=64), server_default='', nullable=False, comment='轮次 ID'),
        sa.Column('user_id', sa.String(length=100), server_default='', nullable=False, comment='提问用户'),
        sa.Column('session_id', sa.String(length=100), server_default='', nullable=False, comment='会话 ID'),
        sa.Column('engine', sa.String(length=24), server_default='', nullable=False, comment='检索通道'),
        sa.Column('doc_name', sa.String(length=256), server_default='', nullable=False, comment='来源文档名'),
        sa.Column('doc_type', sa.String(length=40), server_default='', nullable=False, comment='文档类型'),
        sa.Column('page_number', sa.Integer(), nullable=True, comment='页码'),
        sa.Column('chunk_index', sa.Integer(), nullable=True, comment='片段序号'),
        sa.Column('score', sa.Float(), nullable=True, comment='相关度分数'),
        sa.Column('quote', sa.String(length=400), server_default='', nullable=False, comment='摘句（脱敏截断）'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_citations_trace', 'citations', ['trace_id'])
    op.create_index('ix_citations_user', 'citations', ['user_id'])


def downgrade() -> None:
    op.drop_index('ix_citations_user', table_name='citations')
    op.drop_index('ix_citations_trace', table_name='citations')
    op.drop_table('citations')

    op.drop_index('ix_knowledge_audit_trace', table_name='knowledge_audit_logs')
    op.drop_index('ix_knowledge_audit_intent', table_name='knowledge_audit_logs')
    op.drop_index('ix_knowledge_audit_user', table_name='knowledge_audit_logs')
    op.drop_table('knowledge_audit_logs')

    op.drop_column('chat_messages', 'content_redacted')
