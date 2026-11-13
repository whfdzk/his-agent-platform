"""add appointments, token_usage, model_configs

Revision ID: a3f8c1d92b47
Revises: 2602d7ad2e91
Create Date: 2026-10-24

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'a3f8c1d92b47'
down_revision: Union[str, Sequence[str], None] = '2602d7ad2e91'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── appointments：挂号预约 ──────────────────────────────────────────
    op.create_table(
        'appointments',
        sa.Column('patient_id', sa.BigInteger(), sa.ForeignKey('patients.id', ondelete='SET NULL'), nullable=True, comment='患者 ID（chat 未传 patient_id 时为 NULL）'),
        sa.Column('session_id', sa.String(length=100), nullable=True, comment='关联的会话 ID'),
        sa.Column('appointment_no', sa.String(length=32), nullable=False, comment='预约号，如 APT1A2B3C4D'),
        sa.Column('doctor_name', sa.String(length=100), nullable=False, server_default='', comment='医生姓名及职称'),
        sa.Column('department', sa.String(length=100), nullable=False, server_default='', comment='就诊科室名称'),
        sa.Column('date', sa.String(length=20), nullable=False, server_default='', comment='就诊日期，YYYY-MM-DD'),
        sa.Column('time_slot', sa.String(length=50), nullable=False, server_default='', comment='就诊时段'),
        sa.Column('fee', sa.Float(), nullable=False, server_default='0', comment='挂号费（元）'),
        sa.Column('queue_number', sa.Integer(), nullable=False, server_default='0', comment='就诊序号'),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='confirmed', comment='状态：confirmed=已确认 / cancelled=已取消'),
        sa.Column('diagnosis', sa.String(length=200), nullable=True, comment='关联的主诊断（问诊结论）'),
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False, comment='创建时间'),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False, comment='更新时间'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('appointment_no'),
    )
    op.create_index('ix_appointments_patient', 'appointments', ['patient_id'])
    op.create_index('ix_appointments_session', 'appointments', ['session_id'])

    # ── token_usage：LLM 调用用量 ───────────────────────────────────────
    op.create_table(
        'token_usage',
        sa.Column('user_id', sa.String(length=100), nullable=True, comment='用户 ID'),
        sa.Column('session_id', sa.String(length=100), nullable=True, comment='会话 ID'),
        sa.Column('turn_id', sa.String(length=64), nullable=True, comment='轮次 ID：同一次用户消息触发的多次 LLM 调用共享'),
        sa.Column('worker', sa.String(length=50), nullable=False, server_default='supervisor', comment='调用来源：supervisor/inquiry/drug/knowledge/report/operation'),
        sa.Column('model', sa.String(length=100), nullable=False, server_default='', comment='模型名称'),
        sa.Column('prompt_tokens', sa.Integer(), nullable=False, server_default='0', comment='输入 token 数'),
        sa.Column('completion_tokens', sa.Integer(), nullable=False, server_default='0', comment='输出 token 数'),
        sa.Column('total_tokens', sa.Integer(), nullable=False, server_default='0', comment='总 token 数'),
        sa.Column('latency_ms', sa.Integer(), nullable=False, server_default='0', comment='本次调用耗时（毫秒）'),
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False, comment='创建时间'),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False, comment='更新时间'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_token_usage_turn', 'token_usage', ['turn_id'])
    op.create_index('ix_token_usage_session', 'token_usage', ['session_id'])
    op.create_index('ix_token_usage_worker', 'token_usage', ['worker'])

    # ── model_configs：模型配置 ─────────────────────────────────────────
    op.create_table(
        'model_configs',
        sa.Column('model_type', sa.String(length=20), nullable=False, comment='模型类型：embedding/chat/rerank/multimodal'),
        sa.Column('name', sa.String(length=100), nullable=False, comment='展示名称'),
        sa.Column('base_url', sa.String(length=500), nullable=False, server_default='', comment='API Base URL'),
        sa.Column('api_key', sa.String(length=500), nullable=False, server_default='', comment='API Key（本期明文存储）'),
        sa.Column('model_name', sa.String(length=100), nullable=False, comment='模型名称，如 deepseek-chat'),
        sa.Column('enabled', sa.Boolean(), nullable=False, server_default=sa.text('true'), comment='是否启用'),
        sa.Column('is_default', sa.Boolean(), nullable=False, server_default=sa.text('false'), comment='是否该类型的默认配置'),
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False, comment='创建时间'),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False, comment='更新时间'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_model_configs_type', 'model_configs', ['model_type'])


def downgrade() -> None:
    op.drop_index('ix_model_configs_type', table_name='model_configs')
    op.drop_table('model_configs')
    op.drop_index('ix_token_usage_worker', table_name='token_usage')
    op.drop_index('ix_token_usage_session', table_name='token_usage')
    op.drop_index('ix_token_usage_turn', table_name='token_usage')
    op.drop_table('token_usage')
    op.drop_index('ix_appointments_session', table_name='appointments')
    op.drop_index('ix_appointments_patient', table_name='appointments')
    op.drop_table('appointments')
