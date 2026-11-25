"""事件消费流水表：event_consumptions（消费端幂等落地点）。

Revision ID: c1d5e3f7b2a4
Revises: b8c4e2f6a1d3
Create Date: 2026-11-25

(event_id, consumer) 唯一：Redis Stream 至少一次投递，重复投递到同一消费者
第二次插入命中唯一约束，业务只执行一次。
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "c1d5e3f7b2a4"
down_revision = "b8c4e2f6a1d3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "event_consumptions",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()")),
        sa.Column("event_id", sa.String(200), nullable=False),
        sa.Column("consumer", sa.String(64), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(16), nullable=False, server_default="done"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("error", sa.String(500), nullable=False, server_default=""),
        sa.Column("payload", postgresql.JSONB(), nullable=True),
        sa.Column("handled_at", sa.DateTime(), nullable=True),
    )
    op.create_index("uq_event_event_consumer", "event_consumptions",
                    ["event_id", "consumer"], unique=True)
    op.create_index("ix_event_consumptions_type", "event_consumptions", ["event_type"])


def downgrade() -> None:
    op.drop_index("ix_event_consumptions_type", table_name="event_consumptions")
    op.drop_index("uq_event_event_consumer", table_name="event_consumptions")
    op.drop_table("event_consumptions")
