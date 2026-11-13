"""add patient domain tables and observability/safety tables

患者端数据层 + 全链路追踪 + Safety Engine：
- patient_profiles / vital_metrics / health_reports / report_metrics /
  health_reminders / privacy_consents / data_requests
- agent_traces / agent_spans / safety_events

Revision ID: d7a1f3c9e204
Revises: 9b3c5e1a7d24
Create Date: 2026-11-01

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'd7a1f3c9e204'
down_revision: Union[str, Sequence[str], None] = '9b3c5e1a7d24'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _base_columns() -> list[sa.Column]:
    return [
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
    ]


def upgrade() -> None:
    # ── 健康档案扩展 ────────────────────────────────────────────────
    op.create_table(
        'patient_profiles',
        *_base_columns(),
        sa.Column('patient_id', sa.BigInteger(), nullable=False, comment='患者 ID（一人一条档案）'),
        sa.Column('birth_date', sa.Date(), nullable=True, comment='出生日期'),
        sa.Column('height_cm', sa.Float(), nullable=True, comment='身高（cm）'),
        sa.Column('weight_kg', sa.Float(), nullable=True, comment='体重（kg）'),
        sa.Column('waist_cm', sa.Float(), nullable=True, comment='腰围（cm）'),
        sa.Column('marital_status', sa.String(length=20), nullable=True, comment='婚姻状况'),
        sa.Column('occupation', sa.String(length=50), nullable=True, comment='职业'),
        sa.Column('city', sa.String(length=50), nullable=True, comment='常住城市'),
        sa.Column('emergency_contact_name', sa.String(length=50), nullable=True, comment='紧急联系人'),
        sa.Column('emergency_contact_phone', sa.String(length=20), nullable=True, comment='紧急联系人电话'),
        sa.Column('surgery_history', sa.Text(), nullable=True, comment='手术史'),
        sa.Column('family_history', sa.Text(), nullable=True, comment='家族史'),
        sa.Column('chronic_diseases', sa.Text(), nullable=True, comment='慢性病标签'),
        sa.Column('smoking', sa.String(length=20), nullable=True, comment='吸烟情况'),
        sa.Column('drinking', sa.String(length=20), nullable=True, comment='饮酒情况'),
        sa.ForeignKeyConstraint(['patient_id'], ['patients.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('patient_id', name='uq_patient_profiles_patient'),
    )

    # ── 体征指标 ────────────────────────────────────────────────────
    op.create_table(
        'vital_metrics',
        *_base_columns(),
        sa.Column('patient_id', sa.BigInteger(), nullable=False, comment='患者 ID'),
        sa.Column('metric_type', sa.String(length=32), nullable=False, comment='指标类型'),
        sa.Column('value_primary', sa.Float(), nullable=False, comment='主值'),
        sa.Column('value_secondary', sa.Float(), nullable=True, comment='次值（血压舒张压）'),
        sa.Column('unit', sa.String(length=16), nullable=True, comment='单位'),
        sa.Column('period', sa.String(length=20), nullable=True, comment='测量时段'),
        sa.Column('level', sa.String(length=16), nullable=True, comment='评估结果'),
        sa.Column('source', sa.String(length=20), nullable=True, comment='数据来源'),
        sa.Column('note', sa.String(length=200), nullable=True, comment='备注'),
        sa.Column('measured_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False,
                  comment='测量时间'),
        sa.ForeignKeyConstraint(['patient_id'], ['patients.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_vital_metrics_patient_type_time', 'vital_metrics',
        ['patient_id', 'metric_type', 'measured_at'],
    )

    # ── 报告 ────────────────────────────────────────────────────────
    op.create_table(
        'health_reports',
        *_base_columns(),
        sa.Column('patient_id', sa.BigInteger(), nullable=False, comment='患者 ID'),
        sa.Column('file_name', sa.String(length=255), nullable=False, comment='原始文件名'),
        sa.Column('object_key', sa.String(length=500), nullable=False, comment='RustFS 对象 key'),
        sa.Column('file_size', sa.Integer(), nullable=True, comment='文件大小（字节）'),
        sa.Column('mime_type', sa.String(length=100), nullable=True, comment='MIME 类型'),
        sa.Column('report_type', sa.String(length=32), nullable=True, comment='报告类型'),
        sa.Column('hospital', sa.String(length=100), nullable=True, comment='报告机构'),
        sa.Column('report_date', sa.Date(), nullable=True, comment='报告日期'),
        sa.Column('status', sa.String(length=20), nullable=True, comment='解析状态'),
        sa.Column('parse_engine', sa.String(length=32), nullable=True, comment='解析引擎'),
        sa.Column('summary', sa.Text(), nullable=True, comment='规则化解读摘要'),
        sa.Column('abnormal_count', sa.Integer(), nullable=True, comment='异常指标数量'),
        sa.Column('metric_count', sa.Integer(), nullable=True, comment='识别指标数量'),
        sa.Column('error_message', sa.String(length=300), nullable=True, comment='解析失败原因'),
        sa.Column('parsed_at', sa.DateTime(timezone=True), nullable=True, comment='解析完成时间'),
        sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True, comment='软删除时间'),
        sa.ForeignKeyConstraint(['patient_id'], ['patients.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_health_reports_patient_time', 'health_reports', ['patient_id', 'created_at'])
    op.create_index('ix_health_reports_status', 'health_reports', ['status'])

    op.create_table(
        'report_metrics',
        *_base_columns(),
        sa.Column('report_id', sa.BigInteger(), nullable=False, comment='报告 ID'),
        sa.Column('patient_id', sa.BigInteger(), nullable=False, comment='患者 ID'),
        sa.Column('name', sa.String(length=100), nullable=False, comment='指标名称'),
        sa.Column('code', sa.String(length=50), nullable=True, comment='指标编码'),
        sa.Column('category', sa.String(length=50), nullable=True, comment='分类'),
        sa.Column('value_text', sa.String(length=50), nullable=True, comment='原始值文本'),
        sa.Column('value_num', sa.Float(), nullable=True, comment='数值化取值'),
        sa.Column('unit', sa.String(length=30), nullable=True, comment='单位'),
        sa.Column('ref_low', sa.Float(), nullable=True, comment='参考下限'),
        sa.Column('ref_high', sa.Float(), nullable=True, comment='参考上限'),
        sa.Column('ref_text', sa.String(length=100), nullable=True, comment='参考区间原文'),
        sa.Column('abnormal', sa.String(length=16), nullable=True, comment='是否异常'),
        sa.Column('sort_order', sa.Integer(), nullable=True, comment='展示顺序'),
        sa.ForeignKeyConstraint(['report_id'], ['health_reports.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['patient_id'], ['patients.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_report_metrics_report', 'report_metrics', ['report_id'])
    op.create_index('ix_report_metrics_patient_code', 'report_metrics', ['patient_id', 'code'])

    # ── 健康提醒 ────────────────────────────────────────────────────
    op.create_table(
        'health_reminders',
        *_base_columns(),
        sa.Column('patient_id', sa.BigInteger(), nullable=False, comment='患者 ID'),
        sa.Column('metric_type', sa.String(length=32), nullable=True, comment='关联指标类型'),
        sa.Column('title', sa.String(length=100), nullable=False, comment='提醒标题'),
        sa.Column('remind_time', sa.String(length=8), nullable=True, comment='提醒时间 HH:MM'),
        sa.Column('frequency', sa.String(length=16), nullable=True, comment='频率'),
        sa.Column('weekdays', sa.String(length=20), nullable=True, comment='星期'),
        sa.Column('enabled', sa.Boolean(), nullable=True, comment='是否启用'),
        sa.Column('note', sa.String(length=200), nullable=True, comment='备注'),
        sa.Column('last_fired_at', sa.DateTime(timezone=True), nullable=True, comment='最近触发时间'),
        sa.ForeignKeyConstraint(['patient_id'], ['patients.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_health_reminders_patient', 'health_reminders', ['patient_id'])

    # ── 隐私授权 / 数据请求 ─────────────────────────────────────────
    op.create_table(
        'privacy_consents',
        *_base_columns(),
        sa.Column('patient_id', sa.BigInteger(), nullable=False, comment='患者 ID'),
        sa.Column('scope', sa.String(length=32), nullable=False, comment='授权范围'),
        sa.Column('granted', sa.Boolean(), nullable=True, comment='是否授权'),
        sa.Column('version', sa.String(length=20), nullable=True, comment='隐私政策版本'),
        sa.Column('granted_at', sa.DateTime(timezone=True), nullable=True, comment='授权时间'),
        sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True, comment='撤回时间'),
        sa.ForeignKeyConstraint(['patient_id'], ['patients.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_privacy_consents_patient_scope', 'privacy_consents', ['patient_id', 'scope'])

    op.create_table(
        'data_requests',
        *_base_columns(),
        sa.Column('patient_id', sa.BigInteger(), nullable=False, comment='患者 ID'),
        sa.Column('request_type', sa.String(length=16), nullable=False, comment='export/delete'),
        sa.Column('status', sa.String(length=20), nullable=True, comment='处理状态'),
        sa.Column('object_key', sa.String(length=500), nullable=True, comment='导出文件 key'),
        sa.Column('note', sa.String(length=300), nullable=True, comment='处理说明'),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True, comment='导出文件过期时间'),
        sa.Column('handled_at', sa.DateTime(timezone=True), nullable=True, comment='处理完成时间'),
        sa.ForeignKeyConstraint(['patient_id'], ['patients.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_data_requests_patient', 'data_requests', ['patient_id', 'created_at'])

    # ── 全链路追踪 ──────────────────────────────────────────────────
    op.create_table(
        'agent_traces',
        *_base_columns(),
        sa.Column('trace_id', sa.String(length=32), nullable=False, comment='全链路 trace ID'),
        sa.Column('request_id', sa.String(length=32), nullable=True, comment='HTTP 请求 ID'),
        sa.Column('user_id', sa.String(length=64), nullable=True, comment='用户 ID'),
        sa.Column('session_id', sa.String(length=100), nullable=True, comment='会话 ID'),
        sa.Column('turn_id', sa.String(length=64), nullable=True, comment='单轮 ID'),
        sa.Column('entrypoint', sa.String(length=100), nullable=True, comment='入口'),
        sa.Column('agent_name', sa.String(length=64), nullable=True, comment='主 Agent 名称'),
        sa.Column('agent_version', sa.String(length=32), nullable=True, comment='Agent 版本'),
        sa.Column('prompt_version', sa.String(length=64), nullable=True, comment='Prompt 版本'),
        sa.Column('model_name', sa.String(length=100), nullable=True, comment='模型名称'),
        sa.Column('model_version', sa.String(length=100), nullable=True, comment='模型版本'),
        sa.Column('status', sa.String(length=20), nullable=True, comment='执行状态'),
        sa.Column('error_type', sa.String(length=64), nullable=True, comment='错误类型'),
        sa.Column('latency_ms', sa.Integer(), nullable=True, comment='端到端耗时'),
        sa.Column('prompt_tokens', sa.Integer(), nullable=True, comment='Prompt Token'),
        sa.Column('completion_tokens', sa.Integer(), nullable=True, comment='Completion Token'),
        sa.Column('total_tokens', sa.Integer(), nullable=True, comment='总 Token'),
        sa.Column('tool_calls', sa.Integer(), nullable=True, comment='工具调用次数'),
        sa.Column('tool_names', postgresql.JSONB(astext_type=sa.Text()), nullable=True, comment='工具名列表'),
        sa.Column('agent_calls', sa.Integer(), nullable=True, comment='Agent 调用次数'),
        sa.Column('retrieval_count', sa.Integer(), nullable=True, comment='检索结果条数'),
        sa.Column('retrieval_sources', postgresql.JSONB(astext_type=sa.Text()), nullable=True,
                  comment='检索来源'),
        sa.Column('safety_level', sa.String(length=16), nullable=True, comment='安全等级'),
        sa.Column('safety_result', sa.String(length=20), nullable=True, comment='安全结论'),
        sa.Column('safety_rules', postgresql.JSONB(astext_type=sa.Text()), nullable=True, comment='命中规则'),
        sa.Column('input_chars', sa.Integer(), nullable=True, comment='输入字符数'),
        sa.Column('output_chars', sa.Integer(), nullable=True, comment='输出字符数'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_agent_traces_trace_id', 'agent_traces', ['trace_id'], unique=True)
    op.create_index('ix_agent_traces_user_time', 'agent_traces', ['user_id', 'created_at'])
    op.create_index('ix_agent_traces_session', 'agent_traces', ['session_id'])
    op.create_index('ix_agent_traces_created', 'agent_traces', ['created_at'])

    op.create_table(
        'agent_spans',
        *_base_columns(),
        sa.Column('trace_id', sa.String(length=32), nullable=False, comment='所属 trace'),
        sa.Column('span_id', sa.String(length=32), nullable=False, comment='节点 ID'),
        sa.Column('parent_span_id', sa.String(length=32), nullable=True, comment='父节点 ID'),
        sa.Column('name', sa.String(length=100), nullable=False, comment='节点名'),
        sa.Column('kind', sa.String(length=20), nullable=False, comment='节点类型'),
        sa.Column('agent_name', sa.String(length=64), nullable=True, comment='Agent 名称'),
        sa.Column('agent_version', sa.String(length=32), nullable=True, comment='Agent 版本'),
        sa.Column('tool_name', sa.String(length=64), nullable=True, comment='工具名'),
        sa.Column('model_name', sa.String(length=100), nullable=True, comment='模型名称'),
        sa.Column('model_version', sa.String(length=100), nullable=True, comment='模型版本'),
        sa.Column('status', sa.String(length=20), nullable=True, comment='节点状态'),
        sa.Column('error_type', sa.String(length=64), nullable=True, comment='错误类型'),
        sa.Column('latency_ms', sa.Integer(), nullable=True, comment='耗时'),
        sa.Column('prompt_tokens', sa.Integer(), nullable=True, comment='Prompt Token'),
        sa.Column('completion_tokens', sa.Integer(), nullable=True, comment='Completion Token'),
        sa.Column('total_tokens', sa.Integer(), nullable=True, comment='总 Token'),
        sa.Column('attributes', postgresql.JSONB(astext_type=sa.Text()), nullable=True, comment='脱敏属性'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_agent_spans_trace', 'agent_spans', ['trace_id', 'id'])
    op.create_index('ix_agent_spans_created', 'agent_spans', ['created_at'])

    # ── 安全事件 ────────────────────────────────────────────────────
    op.create_table(
        'safety_events',
        *_base_columns(),
        sa.Column('trace_id', sa.String(length=32), nullable=True, comment='关联 trace'),
        sa.Column('request_id', sa.String(length=32), nullable=True, comment='关联请求'),
        sa.Column('user_id', sa.String(length=64), nullable=True, comment='用户 ID'),
        sa.Column('session_id', sa.String(length=100), nullable=True, comment='会话 ID'),
        sa.Column('stage', sa.String(length=16), nullable=False, comment='审核阶段'),
        sa.Column('level', sa.String(length=16), nullable=False, comment='安全等级'),
        sa.Column('action', sa.String(length=16), nullable=False, comment='处置动作'),
        sa.Column('rule_id', sa.String(length=64), nullable=True, comment='规则 ID'),
        sa.Column('rule_name', sa.String(length=100), nullable=True, comment='规则名称'),
        sa.Column('category', sa.String(length=32), nullable=True, comment='规则分类'),
        sa.Column('snippet', sa.String(length=200), nullable=True, comment='脱敏片段'),
        sa.Column('requires_review', sa.Boolean(), nullable=True, comment='是否需要人工复核'),
        sa.Column('review_status', sa.String(length=20), nullable=True, comment='复核状态'),
        sa.Column('reviewer_id', sa.String(length=64), nullable=True, comment='复核人'),
        sa.Column('review_note', sa.String(length=300), nullable=True, comment='复核意见'),
        sa.Column('reviewed_at', sa.DateTime(timezone=True), nullable=True, comment='复核时间'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_safety_events_created', 'safety_events', ['created_at'])
    op.create_index('ix_safety_events_review', 'safety_events', ['review_status', 'created_at'])
    op.create_index('ix_safety_events_user', 'safety_events', ['user_id', 'created_at'])


def downgrade() -> None:
    op.drop_index('ix_safety_events_user', table_name='safety_events')
    op.drop_index('ix_safety_events_review', table_name='safety_events')
    op.drop_index('ix_safety_events_created', table_name='safety_events')
    op.drop_table('safety_events')

    op.drop_index('ix_agent_spans_created', table_name='agent_spans')
    op.drop_index('ix_agent_spans_trace', table_name='agent_spans')
    op.drop_table('agent_spans')

    op.drop_index('ix_agent_traces_created', table_name='agent_traces')
    op.drop_index('ix_agent_traces_session', table_name='agent_traces')
    op.drop_index('ix_agent_traces_user_time', table_name='agent_traces')
    op.drop_index('ix_agent_traces_trace_id', table_name='agent_traces')
    op.drop_table('agent_traces')

    op.drop_index('ix_data_requests_patient', table_name='data_requests')
    op.drop_table('data_requests')

    op.drop_index('ix_privacy_consents_patient_scope', table_name='privacy_consents')
    op.drop_table('privacy_consents')

    op.drop_index('ix_health_reminders_patient', table_name='health_reminders')
    op.drop_table('health_reminders')

    op.drop_index('ix_report_metrics_patient_code', table_name='report_metrics')
    op.drop_index('ix_report_metrics_report', table_name='report_metrics')
    op.drop_table('report_metrics')

    op.drop_index('ix_health_reports_status', table_name='health_reports')
    op.drop_index('ix_health_reports_patient_time', table_name='health_reports')
    op.drop_table('health_reports')

    op.drop_index('ix_vital_metrics_patient_type_time', table_name='vital_metrics')
    op.drop_table('vital_metrics')

    op.drop_table('patient_profiles')
