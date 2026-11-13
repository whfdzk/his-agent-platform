"""add created_at indexes for stats aggregation

统计接口（token 用量聚合 / 问诊趋势 / 症状 TopN）都按 created_at 过滤，
此前两张表都没有该列索引，数据量增长后会退化为全表扫描。

Revision ID: f1c4d8a72b90
Revises: b5e2f7a91c03
Create Date: 2026-10-28

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'f1c4d8a72b90'
down_revision: Union[str, Sequence[str], None] = 'b5e2f7a91c03'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index('ix_token_usage_created_at', 'token_usage', ['created_at'])
    op.create_index('ix_consultations_created_at', 'consultations', ['created_at'])


def downgrade() -> None:
    op.drop_index('ix_consultations_created_at', table_name='consultations')
    op.drop_index('ix_token_usage_created_at', table_name='token_usage')
