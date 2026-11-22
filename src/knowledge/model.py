from sqlalchemy import String, SmallInteger, Boolean, Index, Integer, Float, Text
from sqlalchemy.orm import Mapped, mapped_column
from src.core.base_model import BaseModel, TenantMixin


class KnowledgeFeedback(BaseModel, TenantMixin):
    """用户对知识问答回答的反馈记录。"""
    __tablename__ = "knowledge_feedback"

    user_id: Mapped[str] = mapped_column(String(64), nullable=False, comment="用户 ID")
    question: Mapped[str] = mapped_column(String(500), nullable=False, comment="用户提问")
    answer_preview: Mapped[str] = mapped_column(String(500), default="", comment="系统回答预览")
    rating: Mapped[int] = mapped_column(SmallInteger, default=0, comment="1=有用, -1=无用, 0=未评价")
    comment: Mapped[str] = mapped_column(String(1000), default="", comment="用户文字反馈")
    intent: Mapped[str] = mapped_column(String(50), default="", comment="检索意图")
    channels: Mapped[str] = mapped_column(String(100), default="", comment="使用的检索通道")

    __table_args__ = (
        Index("ix_knowledge_feedback_user", "user_id"),
        Index("ix_knowledge_feedback_rating", "rating"),
        Index("ix_knowledge_feedback_tenant", "tenant_id"),
    )


class KnowledgeNotification(BaseModel, TenantMixin):
    """知识文档更新通知。"""
    __tablename__ = "knowledge_notifications"

    doc_name: Mapped[str] = mapped_column(String(256), nullable=False, comment="文档名称")
    doc_type: Mapped[str] = mapped_column(String(50), nullable=False, comment="文档类型")
    category: Mapped[str] = mapped_column(String(100), nullable=False, comment="所属分类")
    action: Mapped[str] = mapped_column(String(20), default="upload", comment="操作类型")
    is_read: Mapped[bool] = mapped_column(Boolean, default=False, comment="是否已读")

    __table_args__ = (
        Index("ix_knowledge_notifications_category", "category"),
        Index("ix_knowledge_notifications_is_read", "is_read"),
        Index("ix_knowledge_notifications_tenant", "tenant_id"),
    )


class Citation(BaseModel, TenantMixin):
    """回答引用：一条记录 = 回答中一个被引用的知识片段定位。

    来源是 TraceRecorder.add_retrieval 收集到的检索元信息（文档名/页码/片段序号/
    分数/摘句），因此"模型说依据哪份指南第几页"可以逐条回查原文，
    而不是只在正文里裸写一个文档名。
    """
    __tablename__ = "citations"

    trace_id: Mapped[str] = mapped_column(String(64), default="", comment="关联 trace_id")
    turn_id: Mapped[str] = mapped_column(String(64), default="", comment="轮次 ID")
    user_id: Mapped[str] = mapped_column(String(100), default="", comment="提问用户")
    session_id: Mapped[str] = mapped_column(String(100), default="", comment="会话 ID")
    engine: Mapped[str] = mapped_column(String(24), default="", comment="检索通道：milvus/neo4j/bm25")
    doc_name: Mapped[str] = mapped_column(String(256), default="", comment="来源文档名")
    doc_type: Mapped[str] = mapped_column(String(40), default="", comment="文档类型")
    page_number: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="页码")
    chunk_index: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="片段序号")
    score: Mapped[float | None] = mapped_column(Float, nullable=True, comment="相关度分数")
    score_type: Mapped[str] = mapped_column(
        String(16), default="vector",
        comment="分数语义：vector/rerank/rrf/bm25（量纲不同，不可直接比较）",
    )
    quote: Mapped[str] = mapped_column(String(400), default="", comment="摘句（脱敏截断 120 字）")

    __table_args__ = (
        Index("ix_citations_trace", "trace_id"),
        Index("ix_citations_user", "user_id"),
        Index("ix_citations_tenant", "tenant_id"),
    )