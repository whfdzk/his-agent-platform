# ============================================================
# 可观测性 & 安全审计 Model
#
# - agent_traces：一次请求（一轮对话）的聚合轨迹，回答"谁、用什么 Agent/模型、
#   花了多少时间与 Token、检索了什么、安全审核结论是什么"
# - agent_spans ：链路节点明细（api / supervisor / consultation / tool /
#   retrieval(milvus|neo4j) / llm / safety / final_response）
# - safety_events：Safety Engine 命中记录 + 人工复核状态
#
# 隐私约束：所有文本字段只允许写脱敏摘要（snippet），禁止写完整病历、
# 身份证号、手机号、报告原文。写入统一走 src.core.redaction。
# ============================================================

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Index,
    Integer,
    String,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.core.base_model import BaseModel


class AgentTrace(BaseModel):
    """一轮执行的聚合轨迹（trace 粒度）。"""

    __tablename__ = "agent_traces"

    trace_id: Mapped[str] = mapped_column(String(32), nullable=False, unique=True, comment="全链路 trace ID")
    request_id: Mapped[str] = mapped_column(String(32), default="", comment="HTTP 请求 ID")
    user_id: Mapped[str] = mapped_column(String(64), default="", comment="受控访问的用户 ID（来自 JWT）")
    session_id: Mapped[str] = mapped_column(String(100), default="", comment="会话 ID")
    turn_id: Mapped[str] = mapped_column(String(64), default="", comment="单轮 ID")
    entrypoint: Mapped[str] = mapped_column(String(100), default="", comment="入口：/api/v1/chat/stream 等")
    agent_name: Mapped[str] = mapped_column(String(64), default="supervisor", comment="主 Agent 名称")
    agent_version: Mapped[str] = mapped_column(String(32), default="", comment="Agent 版本")
    prompt_version: Mapped[str] = mapped_column(String(64), default="", comment="Prompt 版本")
    model_name: Mapped[str] = mapped_column(String(100), default="", comment="模型名称")
    model_version: Mapped[str] = mapped_column(String(100), default="", comment="模型版本/指纹")
    status: Mapped[str] = mapped_column(
        String(20), default="running", comment="running/success/error/timeout/blocked/degraded"
    )
    error_type: Mapped[str | None] = mapped_column(String(64), comment="错误类型（异常类名/预算耗尽）")
    latency_ms: Mapped[int] = mapped_column(Integer, default=0, comment="端到端耗时（毫秒）")
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0, comment="Prompt Token")
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0, comment="Completion Token")
    total_tokens: Mapped[int] = mapped_column(Integer, default=0, comment="总 Token")
    tool_calls: Mapped[int] = mapped_column(Integer, default=0, comment="工具调用次数")
    tool_names: Mapped[list | None] = mapped_column(JSONB, comment="调用过的工具名列表")
    agent_calls: Mapped[int] = mapped_column(Integer, default=0, comment="Agent 调用次数")
    retrieval_count: Mapped[int] = mapped_column(Integer, default=0, comment="检索结果条数")
    retrieval_sources: Mapped[list | None] = mapped_column(
        JSONB, comment="检索来源：[{engine, collection, doc_id, score, title}]"
    )
    safety_level: Mapped[str] = mapped_column(
        String(16), default="info", comment="安全等级：info/warn/high/critical"
    )
    safety_result: Mapped[str] = mapped_column(
        String(20), default="allow", comment="安全结论：allow/warn/block/escalate"
    )
    safety_rules: Mapped[list | None] = mapped_column(JSONB, comment="命中的规则 ID 列表")
    input_chars: Mapped[int] = mapped_column(Integer, default=0, comment="输入字符数（不存原文）")
    output_chars: Mapped[int] = mapped_column(Integer, default=0, comment="输出字符数（不存原文）")

    __table_args__ = (
        Index("ix_agent_traces_user_time", "user_id", "created_at"),
        Index("ix_agent_traces_session", "session_id"),
        Index("ix_agent_traces_created", "created_at"),
    )


class AgentSpan(BaseModel):
    """链路节点明细。attributes 只放白名单字段，文本必须先脱敏。"""

    __tablename__ = "agent_spans"

    trace_id: Mapped[str] = mapped_column(String(32), nullable=False, comment="所属 trace")
    span_id: Mapped[str] = mapped_column(String(32), nullable=False, comment="节点 ID")
    parent_span_id: Mapped[str | None] = mapped_column(String(32), comment="父节点 ID")
    name: Mapped[str] = mapped_column(String(100), nullable=False, comment="节点名")
    kind: Mapped[str] = mapped_column(
        String(20), nullable=False, comment="api/agent/tool/llm/retrieval/safety/final_response"
    )
    agent_name: Mapped[str | None] = mapped_column(String(64), comment="Agent 名称")
    agent_version: Mapped[str | None] = mapped_column(String(32), comment="Agent 版本")
    tool_name: Mapped[str | None] = mapped_column(String(64), comment="工具名")
    model_name: Mapped[str | None] = mapped_column(String(100), comment="模型名称")
    model_version: Mapped[str | None] = mapped_column(String(100), comment="模型版本/指纹")
    status: Mapped[str] = mapped_column(String(20), default="ok", comment="ok/error/timeout/blocked")
    error_type: Mapped[str | None] = mapped_column(String(64), comment="错误类型")
    latency_ms: Mapped[int] = mapped_column(Integer, default=0, comment="耗时（毫秒）")
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0, comment="Prompt Token")
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0, comment="Completion Token")
    total_tokens: Mapped[int] = mapped_column(Integer, default=0, comment="总 Token")
    attributes: Mapped[dict | None] = mapped_column(JSONB, comment="脱敏后的结构化属性")

    __table_args__ = (
        Index("ix_agent_spans_trace", "trace_id", "id"),
        Index("ix_agent_spans_created", "created_at"),
    )


class SafetyEvent(BaseModel):
    """Safety Engine 安全事件：命中危险信号或输出违规时落库，支持人工复核。"""

    __tablename__ = "safety_events"

    trace_id: Mapped[str] = mapped_column(String(32), default="", comment="关联 trace")
    request_id: Mapped[str] = mapped_column(String(32), default="", comment="关联请求")
    user_id: Mapped[str] = mapped_column(String(64), default="", comment="受控访问的用户 ID")
    session_id: Mapped[str] = mapped_column(String(100), default="", comment="会话 ID")
    stage: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="审核阶段：input/output/tool"
    )
    level: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="info/warn/high/critical"
    )
    action: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="处置：allow/warn/block/escalate"
    )
    rule_id: Mapped[str] = mapped_column(String(64), default="", comment="命中规则 ID")
    rule_name: Mapped[str] = mapped_column(String(100), default="", comment="规则名称")
    category: Mapped[str] = mapped_column(String(32), default="general", comment="规则分类")
    snippet: Mapped[str | None] = mapped_column(String(200), comment="脱敏后的片段（≤200 字）")
    requires_review: Mapped[bool] = mapped_column(Boolean, default=False, comment="是否需要人工复核")
    review_status: Mapped[str] = mapped_column(
        String(20), default="none", comment="none/pending/approved/rejected"
    )
    reviewer_id: Mapped[str | None] = mapped_column(String(64), comment="复核人 ID")
    review_note: Mapped[str | None] = mapped_column(String(300), comment="复核意见")
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), comment="复核时间")

    __table_args__ = (
        Index("ix_safety_events_created", "created_at"),
        Index("ix_safety_events_review", "review_status", "created_at"),
        Index("ix_safety_events_user", "user_id", "created_at"),
    )
