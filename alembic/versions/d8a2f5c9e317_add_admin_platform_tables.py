"""add admin platform tables

管理端 P0/P1/P2 平台域表：
- agents / prompts / prompt_versions：Agent 与 Prompt 版本管理
- system_configs：系统参数 / 开关 / 字典
- membership_plans / membership_orders / user_memberships：会员域
- patients.enabled：用户账号状态管理（管理端封禁开关）

Revision ID: d8a2f5c9e317
Revises: c1d9e4b7a305
Create Date: 2026-11-02

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'd8a2f5c9e317'
down_revision: Union[str, Sequence[str], None] = 'c1d9e4b7a305'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _base_columns() -> list[sa.Column]:
    return [
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
    ]


def upgrade() -> None:
    # 用户管理：账号启用/禁用开关（管理端封禁）
    op.add_column(
        'patients',
        sa.Column('enabled', sa.Boolean(), server_default=sa.true(), nullable=True, comment='账号是否启用（管理端封禁开关）'),
    )

    # Agent 管理
    op.create_table(
        'agents',
        *_base_columns(),
        sa.Column('code', sa.String(length=64), nullable=False, comment='唯一编码，如 inquiry/drug/knowledge'),
        sa.Column('name', sa.String(length=100), nullable=False, comment='展示名称'),
        sa.Column('agent_type', sa.String(length=20), server_default='worker', nullable=True, comment='类型：supervisor/worker'),
        sa.Column('description', sa.String(length=500), server_default='', nullable=True, comment='职责描述'),
        sa.Column('avatar', sa.String(length=200), server_default='', nullable=True, comment='图标/头像标识'),
        sa.Column('status', sa.String(length=20), server_default='draft', nullable=True, comment='draft/published/disabled'),
        sa.Column('config', postgresql.JSONB(), nullable=True, comment='扩展配置（模型覆盖、工具开关等）'),
        sa.Column('published_at', sa.String(length=32), nullable=True, comment='最近发布时间（ISO 字符串）'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('uq_agents_code', 'agents', ['code'], unique=True)
    op.create_index('ix_agents_status', 'agents', ['status'])

    # Prompt 版本管理
    op.create_table(
        'prompts',
        *_base_columns(),
        sa.Column('key', sa.String(length=64), nullable=False, comment='业务键，如 supervisor.system'),
        sa.Column('name', sa.String(length=100), nullable=False, comment='展示名称'),
        sa.Column('scene', sa.String(length=50), server_default='agent', nullable=True, comment='使用场景：agent/tool/audit/other'),
        sa.Column('description', sa.String(length=500), server_default='', nullable=True, comment='用途说明'),
        sa.Column('enabled', sa.Boolean(), server_default=sa.true(), nullable=True, comment='是否启用'),
        sa.Column('active_version', sa.Integer(), server_default='0', nullable=True, comment='当前激活的版本号（0=尚未激活）'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('uq_prompts_key', 'prompts', ['key'], unique=True)

    op.create_table(
        'prompt_versions',
        *_base_columns(),
        sa.Column('prompt_id', sa.BigInteger(), nullable=False, comment='所属 Prompt ID'),
        sa.Column('version', sa.Integer(), nullable=False, comment='版本号，从 1 递增'),
        sa.Column('content', sa.Text(), nullable=False, comment='提示词正文'),
        sa.Column('change_note', sa.String(length=300), server_default='', nullable=True, comment='变更说明'),
        sa.Column('created_by', sa.String(length=64), server_default='', nullable=True, comment='创建人（管理员用户名）'),
        sa.ForeignKeyConstraint(['prompt_id'], ['prompts.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('uq_prompt_versions_prompt_version', 'prompt_versions', ['prompt_id', 'version'], unique=True)

    # 系统配置
    op.create_table(
        'system_configs',
        *_base_columns(),
        sa.Column('key', sa.String(length=100), nullable=False, comment='配置键，如 chat.max_turns'),
        sa.Column('value', sa.Text(), server_default='', nullable=True, comment='配置值（字典存 JSON 数组）'),
        sa.Column('config_type', sa.String(length=20), server_default='param', nullable=True, comment='param/switch/dict'),
        sa.Column('group', sa.String(length=50), server_default='general', nullable=True, comment='分组'),
        sa.Column('name', sa.String(length=100), nullable=False, comment='展示名称'),
        sa.Column('description', sa.String(length=500), server_default='', nullable=True, comment='说明'),
        sa.Column('enabled', sa.Boolean(), server_default=sa.true(), nullable=True, comment='是否生效'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('uq_system_configs_key', 'system_configs', ['key'], unique=True)
    op.create_index('ix_system_configs_group', 'system_configs', ['group'])

    # 会员域（P2）
    op.create_table(
        'membership_plans',
        *_base_columns(),
        sa.Column('name', sa.String(length=100), nullable=False, comment='套餐名称'),
        sa.Column('price_cents', sa.Integer(), server_default='0', nullable=True, comment='价格（分）'),
        sa.Column('duration_days', sa.Integer(), server_default='30', nullable=True, comment='有效期（天）'),
        sa.Column('benefits', sa.Text(), server_default='', nullable=True, comment='权益说明，JSON 数组字符串'),
        sa.Column('enabled', sa.Boolean(), server_default=sa.true(), nullable=True, comment='是否上架'),
        sa.Column('sort_order', sa.Integer(), server_default='0', nullable=True, comment='展示排序'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_membership_plans_enabled', 'membership_plans', ['enabled'])

    op.create_table(
        'membership_orders',
        *_base_columns(),
        sa.Column('order_no', sa.String(length=32), nullable=False, comment='订单号'),
        sa.Column('user_id', sa.String(length=100), nullable=False, comment='购买用户 ID'),
        sa.Column('plan_id', sa.BigInteger(), nullable=False, comment='套餐 ID'),
        sa.Column('plan_name', sa.String(length=100), server_default='', nullable=True, comment='下单时套餐名称快照'),
        sa.Column('amount_cents', sa.Integer(), server_default='0', nullable=True, comment='实付金额（分）'),
        sa.Column('status', sa.String(length=20), server_default='pending', nullable=True, comment='pending/paid/cancelled/refunded'),
        sa.ForeignKeyConstraint(['plan_id'], ['membership_plans.id'], ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('uq_membership_orders_no', 'membership_orders', ['order_no'], unique=True)
    op.create_index('ix_membership_orders_user', 'membership_orders', ['user_id'])
    op.create_index('ix_membership_orders_status', 'membership_orders', ['status'])

    op.create_table(
        'user_memberships',
        *_base_columns(),
        sa.Column('user_id', sa.String(length=100), nullable=False, comment='用户 ID'),
        sa.Column('plan_id', sa.BigInteger(), nullable=False, comment='套餐 ID'),
        sa.Column('plan_name', sa.String(length=100), server_default='', nullable=True, comment='套餐名称快照'),
        sa.Column('start_at', sa.String(length=32), nullable=True, comment='开始时间 ISO'),
        sa.Column('expire_at', sa.String(length=32), nullable=True, comment='到期时间 ISO'),
        sa.Column('status', sa.String(length=20), server_default='active', nullable=True, comment='active/expired'),
        sa.ForeignKeyConstraint(['plan_id'], ['membership_plans.id'], ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('uq_user_memberships_user', 'user_memberships', ['user_id'], unique=True)


def downgrade() -> None:
    op.drop_index('uq_user_memberships_user', table_name='user_memberships')
    op.drop_table('user_memberships')
    op.drop_index('ix_membership_orders_status', table_name='membership_orders')
    op.drop_index('ix_membership_orders_user', table_name='membership_orders')
    op.drop_index('uq_membership_orders_no', table_name='membership_orders')
    op.drop_table('membership_orders')
    op.drop_index('ix_membership_plans_enabled', table_name='membership_plans')
    op.drop_table('membership_plans')
    op.drop_index('ix_system_configs_group', table_name='system_configs')
    op.drop_index('uq_system_configs_key', table_name='system_configs')
    op.drop_table('system_configs')
    op.drop_index('uq_prompt_versions_prompt_version', table_name='prompt_versions')
    op.drop_table('prompt_versions')
    op.drop_index('uq_prompts_key', table_name='prompts')
    op.drop_table('prompts')
    op.drop_index('ix_agents_status', table_name='agents')
    op.drop_index('uq_agents_code', table_name='agents')
    op.drop_table('agents')
    op.drop_column('patients', 'enabled')
