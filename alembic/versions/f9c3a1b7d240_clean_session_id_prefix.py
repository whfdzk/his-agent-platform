"""clean session_id polluted with user prefix

历史问诊把 thread_id（"<user_id>:<session_id>"）当成 session_id 写入了
consultations / appointments，前端回放历史时会再拼一层 user 前缀，
导致打开历史会话看不到消息。这里把已有数据改回前端原始 session_id。

Revision ID: f9c3a1b7d240
Revises: e2b7c4d81f56
Create Date: 2026-11-01 10:20:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'f9c3a1b7d240'
down_revision: Union[str, Sequence[str], None] = 'e2b7c4d81f56'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """去掉 consultations / appointments 中 session_id 的 "<user_id>:" 前缀。"""
    for table in ("consultations", "appointments"):
        op.execute(
            f"UPDATE {table} SET session_id = split_part(session_id, ':', 2) "
            f"WHERE session_id LIKE '%:%' AND split_part(session_id, ':', 2) <> ''"
        )


def downgrade() -> None:
    """不可逆：前缀是脏数据，恢复没有意义。"""
    pass
