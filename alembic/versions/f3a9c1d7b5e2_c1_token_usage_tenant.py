"""C1：token_usage 加租户列

Revision ID: f3a9c1d7b5e2
Revises: e5b2f7c9a3d1
Create Date: 2026-11-21

背景：管理端「数据统计」要按租户看调用量与 Token。前两批 40 张表里没有
token_usage（它由 core 层埋点异步写入，不在业务模块），这里单独补一列。

存量行全部归默认租户 1（与 C1 既定口径一致：之前只有一家在用）。
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "f3a9c1d7b5e2"
down_revision = "e5b2f7c9a3d1"
branch_labels = None
depends_on = None

TABLE = "token_usage"


def upgrade() -> None:
    op.add_column(
        TABLE,
        sa.Column(
            "tenant_id",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("1"),
            comment="租户 ID",
        ),
    )
    op.create_index(f"ix_{TABLE}_tenant", TABLE, ["tenant_id"])


def downgrade() -> None:
    op.drop_index(f"ix_{TABLE}_tenant", table_name=TABLE)
    op.drop_column(TABLE, "tenant_id")
