"""add knowledge_feedback / knowledge_notifications

src/knowledge/model.py 定义了两张表，但一直没被 alembic 导入、也没建过，
导致 /knowledge/feedback、/knowledge/feedback/stats、/knowledge/notifications
三个接口直接 500（UndefinedTable），管理端的反馈统计恒为空。

Revision ID: b4e8f2a61c93
Revises: f9c3a1b7d240
Create Date: 2026-11-01 12:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b4e8f2a61c93'
down_revision: Union[str, Sequence[str], None] = 'f9c3a1b7d240'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """创建知识反馈表与知识更新通知表（幂等：已存在则跳过）。"""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = set(inspector.get_table_names())

    if 'knowledge_feedback' not in existing:
        op.create_table(
            'knowledge_feedback',
            sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
            sa.Column('user_id', sa.String(length=64), nullable=False, comment='用户 ID'),
            sa.Column('question', sa.String(length=500), nullable=False, comment='用户提问'),
            sa.Column('answer_preview', sa.String(length=500), nullable=False,
                      server_default='', comment='系统回答预览'),
            sa.Column('rating', sa.SmallInteger(), nullable=False,
                      server_default='0', comment='1=有用, -1=无用, 0=未评价'),
            sa.Column('comment', sa.String(length=1000), nullable=False,
                      server_default='', comment='用户文字反馈'),
            sa.Column('intent', sa.String(length=50), nullable=False,
                      server_default='', comment='检索意图'),
            sa.Column('channels', sa.String(length=100), nullable=False,
                      server_default='', comment='使用的检索通道'),
            sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'),
                      nullable=False, comment='创建时间'),
            sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'),
                      nullable=False, comment='更新时间'),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index('ix_knowledge_feedback_user', 'knowledge_feedback', ['user_id'])
        op.create_index('ix_knowledge_feedback_rating', 'knowledge_feedback', ['rating'])

    if 'knowledge_notifications' not in existing:
        op.create_table(
            'knowledge_notifications',
            sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
            sa.Column('doc_name', sa.String(length=256), nullable=False, comment='文档名称'),
            sa.Column('doc_type', sa.String(length=50), nullable=False, comment='文档类型'),
            sa.Column('category', sa.String(length=100), nullable=False, comment='所属分类'),
            sa.Column('action', sa.String(length=20), nullable=False,
                      server_default='upload', comment='操作类型'),
            sa.Column('is_read', sa.Boolean(), nullable=False,
                      server_default=sa.text('false'), comment='是否已读'),
            sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'),
                      nullable=False, comment='创建时间'),
            sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'),
                      nullable=False, comment='更新时间'),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index('ix_knowledge_notifications_category', 'knowledge_notifications', ['category'])
        op.create_index('ix_knowledge_notifications_is_read', 'knowledge_notifications', ['is_read'])


def downgrade() -> None:
    op.drop_index('ix_knowledge_notifications_is_read', table_name='knowledge_notifications')
    op.drop_index('ix_knowledge_notifications_category', table_name='knowledge_notifications')
    op.drop_table('knowledge_notifications')
    op.drop_index('ix_knowledge_feedback_rating', table_name='knowledge_feedback')
    op.drop_index('ix_knowledge_feedback_user', table_name='knowledge_feedback')
    op.drop_table('knowledge_feedback')
