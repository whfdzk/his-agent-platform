"""add conversations and chat_messages

Revision ID: b5e2f7a91c03
Revises: c7e2a9f41b30
Create Date: 2026-10-24

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'b5e2f7a91c03'
down_revision: Union[str, Sequence[str], None] = 'c7e2a9f41b30'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'conversations',
        sa.Column('id', sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column('user_id', sa.String(length=100), nullable=False, comment='用户 ID'),
        sa.Column('session_id', sa.String(length=100), nullable=False, comment='会话 ID（前端生成）'),
        sa.Column('title', sa.String(length=200), server_default='', comment='会话标题（首条用户消息截取）'),
        sa.Column('status', sa.String(length=20), server_default='active', comment='状态：active/closed'),
        sa.Column('last_seq', sa.BigInteger(), server_default='0', comment='会话内事件序号游标'),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), comment='创建时间'),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now(), comment='更新时间'),
        comment='聊天会话',
    )
    op.create_index(
        'uq_conversation_user_session', 'conversations', ['user_id', 'session_id'], unique=True
    )

    op.create_table(
        'chat_messages',
        sa.Column('id', sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column('conversation_id', sa.BigInteger(), nullable=False, comment='所属会话 ID'),
        sa.Column('seq', sa.BigInteger(), nullable=False, comment='会话内递增序号，用于断线续传'),
        sa.Column('turn_id', sa.String(length=64), nullable=True, comment='轮次 ID'),
        sa.Column('client_message_id', sa.String(length=64), nullable=True, comment='客户端幂等键'),
        sa.Column('role', sa.String(length=20), nullable=False, comment='user/assistant/system'),
        sa.Column('content', sa.Text(), server_default='', comment='消息文本内容'),
        sa.Column('event_type', sa.String(length=20), server_default='token',
                  comment='消息类型：token/clarify/appointment/error/interrupted'),
        sa.Column('appointment_data', postgresql.JSONB(), nullable=True, comment='挂号事件结构化数据'),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), comment='创建时间'),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now(), comment='更新时间'),
        comment='聊天消息（与 SSE 事件一一对应）',
    )
    op.create_index(
        'uq_chat_message_conv_seq', 'chat_messages', ['conversation_id', 'seq'], unique=True
    )
    op.create_index(
        'ix_chat_message_client_msg_id', 'chat_messages', ['client_message_id']
    )


def downgrade() -> None:
    op.drop_index('ix_chat_message_client_msg_id', table_name='chat_messages')
    op.drop_index('uq_chat_message_conv_seq', table_name='chat_messages')
    op.drop_table('chat_messages')
    op.drop_index('uq_conversation_user_session', table_name='conversations')
    op.drop_table('conversations')
