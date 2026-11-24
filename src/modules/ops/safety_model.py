# ============================================================
# 安全审计模型
#
# 只保留 safety_events 一张表：Safety Engine 命中记录 + 人工复核状态。
# 链路轨迹（原 agent_traces / agent_spans）已统一到 Langfuse，
# 见 src/core/langfuse_tracing.py，不再落数据库。
#
# 隐私约束：文本字段只允许写脱敏摘要（snippet），禁止写完整病历、
# 身份证号、手机号、报告原文。写入统一走 src.core.redaction。
# ============================================================

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from src.core.base_model import BaseModel, TenantMixin


class SafetyEvent(BaseModel, TenantMixin):
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
        Index("ix_safety_events_tenant", "tenant_id"),
    )
