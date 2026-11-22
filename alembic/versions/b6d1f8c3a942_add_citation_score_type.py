"""add citations.score_type

引用分数语义：vector / rerank / rrf / bm25。
实测出现过 21.66（RRF 融合分）与 0.56（余弦）并存，不标类型就无法设阈值或按分数排序。

Revision ID: b6d1f8c3a942
Revises: a7c3b9e214d8
Create Date: 2026-11-20

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'b6d1f8c3a942'
down_revision: Union[str, Sequence[str], None] = 'a7c3b9e214d8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'citations',
        sa.Column(
            'score_type', sa.String(length=16), server_default='vector', nullable=False,
            comment='分数语义：vector/rerank/rrf/bm25（量纲不同，不可直接比较）',
        ),
    )


def downgrade() -> None:
    op.drop_column('citations', 'score_type')
