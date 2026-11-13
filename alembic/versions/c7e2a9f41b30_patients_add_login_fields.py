"""patients: add phone/password_hash for patient login

Revision ID: c7e2a9f41b30
Revises: a3f8c1d92b47
Create Date: 2026-10-24

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'c7e2a9f41b30'
down_revision: Union[str, Sequence[str], None] = 'a3f8c1d92b47'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # phone 移到 name 之后仅为语义顺序，PostgreSQL 无所谓，直接加列即可
    op.add_column(
        'patients',
        sa.Column('password_hash', sa.String(length=200), nullable=True, comment='登录密码 bcrypt 哈希（为空表示未注册）'),
    )
    op.create_index('ix_patients_phone', 'patients', ['phone'])


def downgrade() -> None:
    op.drop_index('ix_patients_phone', table_name='patients')
    op.drop_column('patients', 'password_hash')
