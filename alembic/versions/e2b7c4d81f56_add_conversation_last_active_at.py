"""add conversations.last_active_at

会话列表需要按最后活跃时间排序，模型早已声明该字段但迁移里漏建，
导致 ORM 查询 conversations 直接报 UndefinedColumn，历史消息读取降级、
会话时间恒为空。

Revision ID: e2b7c4d81f56
Revises: d7a1f3c9e204
Create Date: 2026-11-01 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e2b7c4d81f56'
down_revision: Union[str, Sequence[str], None] = 'd7a1f3c9e204'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """补充 conversations.last_active_at（幂等：已存在则跳过）。"""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {c['name'] for c in inspector.get_columns('conversations')}
    if 'last_active_at' not in columns:
        op.add_column(
            'conversations',
            sa.Column('last_active_at', sa.DateTime(), nullable=True,
                      comment='最后活跃时间'),
        )
    # 历史会话用 updated_at 回填，避免列表排序全部落在 NULL
    op.execute(
        "UPDATE conversations SET last_active_at = updated_at "
        "WHERE last_active_at IS NULL"
    )


def downgrade() -> None:
    op.drop_column('conversations', 'last_active_at')
