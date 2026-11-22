# src/modules/ops/audit_model.py
#
# 知识检索审计落库表。
#
# 背景：src/knowledge/audit.py 的 QueryAuditLog 此前只写 loguru 日志，
# 日志轮转/容器重建后审计链断裂，"谁在什么时候查了什么、答了什么、有没有文档
# 支撑"无法回溯。医疗场景要求可查证，因此改为：日志照旧 + 落库留痕。
#
# 只存脱敏截断后的片段（≤200 字），不存完整问答原文。

from __future__ import annotations

from sqlalchemy import Boolean, Float, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.core.base_model import BaseModel


class KnowledgeAuditLog(BaseModel):
    """一次知识检索的完整链路记录（提问 → 通道 → 耗时 → 是否履约）。"""
    __tablename__ = "knowledge_audit_logs"

    trace_id: Mapped[str] = mapped_column(String(64), default="", comment="关联的 trace_id")
    request_id: Mapped[str] = mapped_column(String(64), default="", comment="请求 ID")
    user_id: Mapped[str] = mapped_column(String(100), default="", comment="提问用户")
    role: Mapped[str] = mapped_column(String(20), default="patient", comment="提问角色")
    intent: Mapped[str] = mapped_column(
        String(32), default="", comment="检索意图：doc_rag/graph_rag/nl2sql/multi/agentic"
    )
    channels: Mapped[list | None] = mapped_column(
        JSONB, nullable=True, comment="实际使用的检索通道列表"
    )
    question: Mapped[str] = mapped_column(Text, default="", comment="提问（脱敏截断 200 字）")
    answer_preview: Mapped[str] = mapped_column(
        Text, default="", comment="回答预览（脱敏截断 200 字）"
    )
    duration_ms: Mapped[float] = mapped_column(Float, default=0.0, comment="检索+生成耗时（毫秒）")
    grounded: Mapped[bool | None] = mapped_column(
        Boolean, nullable=True, comment="引用核查结论：True=有文档支撑"
    )
    hit_count: Mapped[int] = mapped_column(Integer, default=0, comment="命中片段数")

    __table_args__ = (
        Index("ix_knowledge_audit_user", "user_id"),
        Index("ix_knowledge_audit_intent", "intent"),
        Index("ix_knowledge_audit_trace", "trace_id"),
    )
