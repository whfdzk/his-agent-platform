"""add admins table and model_configs.priority

管理端鉴权 + 模型故障转移：
- admins：独立的后台管理员账号（与 patients 隔离），供 /api/v1/admin/auth/login 使用
- model_configs.priority：同类型内的故障转移顺序，主模型失败时按此链依次切换

Revision ID: 9b3c5e1a7d24
Revises: f1c4d8a72b90
Create Date: 2026-10-29

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '9b3c5e1a7d24'
down_revision: Union[str, Sequence[str], None] = 'f1c4d8a72b90'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'admins',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.Column('username', sa.String(length=64), nullable=False, comment='登录账号'),
        sa.Column('password_hash', sa.String(length=255), nullable=False, comment='密码哈希'),
        sa.Column('display_name', sa.String(length=100), nullable=True, comment='展示名称'),
        sa.Column('role', sa.String(length=32), nullable=True, comment='角色：admin（预留 viewer/operator）'),
        sa.Column('enabled', sa.Boolean(), nullable=True, comment='是否启用'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_admins_username', 'admins', ['username'], unique=True)

    op.add_column(
        'model_configs',
        sa.Column('priority', sa.Integer(), nullable=True, comment='故障转移优先级，数字越小越先尝试'),
    )
    # 存量行补默认值：否则 NULL 会让排序把老配置全排到最前
    op.execute("UPDATE model_configs SET priority = 100 WHERE priority IS NULL")
    op.create_index('ix_model_configs_type_priority', 'model_configs', ['model_type', 'priority'])


def downgrade() -> None:
    op.drop_index('ix_model_configs_type_priority', table_name='model_configs')
    op.drop_column('model_configs', 'priority')
    op.drop_index('ix_admins_username', table_name='admins')
    op.drop_table('admins')
