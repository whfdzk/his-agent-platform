# src/modules/chat/model.py
#
# 聊天消息持久化模型。
# PG 为 source of truth：Redis（InquiryState/checkpointer，TTL 1h）只做热缓存，
# Redis 故障或过期后历史从这两张表恢复，并满足医疗场景的审计留存要求。

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.core.base_model import BaseModel, TenantMixin


class Conversation(BaseModel, TenantMixin):
    """会话：user_id + session_id 唯一。"""
    __tablename__ = "conversations"

    user_id: Mapped[str] = mapped_column(String(100), nullable=False, comment="用户 ID")
    session_id: Mapped[str] = mapped_column(String(100), nullable=False, comment="会话 ID（前端生成）")
    title: Mapped[str] = mapped_column(String(200), default="", comment="会话标题（首条用户消息截取）")
    status: Mapped[str] = mapped_column(String(20), default="active", comment="状态：active/closed")
    last_seq: Mapped[int] = mapped_column(BigInteger, default=0, comment="会话内事件序号游标")
    last_active_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="最后活跃时间")

    __table_args__ = (
        Index("uq_conversation_user_session", "user_id", "session_id", unique=True),
        Index("ix_conversation_last_active", "last_active_at"),
        Index("ix_conversations_tenant", "tenant_id"),
        Index("ix_conversations_tenant_user_updated", "tenant_id", "user_id", "updated_at"),
    )


class ChatMessage(BaseModel, TenantMixin):
    """消息：与 SSE 事件一一对应（token 聚合后的完整消息粒度）。"""
    __tablename__ = "chat_messages"

    conversation_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="所属会话 ID（conversations.id）"
    )
    seq: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="会话内递增序号，用于断线续传")
    turn_id: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="轮次 ID（同一次用户消息）")
    client_message_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="客户端幂等键，重复提交去重"
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False, comment="user/assistant/system")
    content: Mapped[str] = mapped_column(Text, default="", comment="消息文本内容（原文，仅限临床审计追溯）")
    # PHI 双列：原文列保留（病历追溯与纠纷举证需要原貌），对外一律读脱敏列，
    # 避免管理端列表、导出、二次训练语料把身份证/手机号原样带走
    content_redacted: Mapped[str] = mapped_column(
        Text, default="", server_default="",
        comment="PHI 脱敏后的消息文本（对外展示/导出/统计使用）",
    )
    event_type: Mapped[str] = mapped_column(
        String(20), default="token", comment="消息类型：token/clarify/appointment/error/interrupted"
    )
    appointment_data: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True, comment="挂号成功时的结构化数据（appointment 事件负载）"
    )

    __table_args__ = (
        Index("uq_chat_message_conv_seq", "conversation_id", "seq", unique=True),
        Index("ix_chat_message_client_msg_id", "client_message_id"),
        Index("ix_chat_messages_tenant", "tenant_id"),
    )
