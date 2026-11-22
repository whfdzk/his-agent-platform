# src/modules/handoff/model.py
#
# C3 人工接管：工单表。
#
# 背景：AI 不是万能的。患者明确要求"转人工"、或模型自己判断这个情况接不住时，
# 需要把对话交给坐席/医生继续。此前系统只有"agent 之间 handoff"（P0-2）与
# "挂号草稿确认"（B2），没有任何"人接力"的载体：患者喊人工也没有入口，
# 坐席不知道谁在等、等了多久、上一句说了什么。
#
# 设计取舍：
#   1. 工单不存对话正文 —— 正文在 chat_messages（source of truth），工单只记
#      "谁、哪个会话、为什么、现在归谁、到哪一步"，避免同一段话存两份后对不上。
#   2. 状态机刻意做小：open（待认领）→ claimed（已认领）→ resolved（已回复/已解决）；
#      closed 是坐席主动关；cancelled 是患者撤回。没有 assigned→transfer 的复杂流转，
#      坐席台先按"能接、能回、能关"跑起来。
#   3. 同一会话同时只允许一张未结束工单：患者重复点"转人工"不应产生 5 张单，
#      否则坐席台被重复项刷屏，且患者会收到多路回复。

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.core.base_model import BaseModel, TenantMixin

STATUS_OPEN = "open"
STATUS_CLAIMED = "claimed"
STATUS_RESOLVED = "resolved"
STATUS_CLOSED = "closed"
STATUS_CANCELLED = "cancelled"

# 未结束 = 患者还在等，或坐席正在处理
ACTIVE_STATUSES = (STATUS_OPEN, STATUS_CLAIMED)

PRIORITIES = ("low", "normal", "high", "urgent")
REASONS = ("patient_request", "model_escalate", "emergency", "unclear")


class HandoffTicket(BaseModel, TenantMixin):
    """一张人工接管工单。"""
    __tablename__ = "handoff_tickets"

    ticket_no: Mapped[str] = mapped_column(String(24), default="", comment="工单号（对外可读）")
    patient_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="申请人（patients.id）")
    session_id: Mapped[str] = mapped_column(String(100), nullable=False, comment="会话 ID")
    conversation_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="conversations.id（会话尚未落库时为空）"
    )
    status: Mapped[str] = mapped_column(
        String(20), default=STATUS_OPEN, comment="open/claimed/resolved/closed/cancelled"
    )
    priority: Mapped[str] = mapped_column(
        String(16), default="normal", comment="low/normal/high/urgent"
    )
    reason: Mapped[str] = mapped_column(
        String(32), default="patient_request", comment="patient_request/model_escalate/emergency/unclear"
    )
    summary: Mapped[str] = mapped_column(
        Text, default="", comment="患者一句话诉求 / 模型给出的升级理由（截断 500 字）"
    )
    payload: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True, comment="附加上下文：最近症状、分级、已生成的草稿动作等"
    )
    assigned_to: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="认领人（patients.id；坐席/医生）"
    )
    assigned_name: Mapped[str] = mapped_column(String(64), default="", comment="认领人姓名快照")
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="认领时间")
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="解决时间")
    resolution: Mapped[str] = mapped_column(
        Text, default="", comment="解决说明（坐席填写，患者可见）"
    )
    message_count: Mapped[int] = mapped_column(
        Integer, default=0, comment="本工单下坐席已回的消息数"
    )
    last_message_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="最后一条坐席回复时间"
    )

    __table_args__ = (
        Index("ix_handoff_ticket_status", "status"),
        Index("ix_handoff_ticket_patient", "patient_id"),
        Index("ix_handoff_ticket_session", "patient_id", "session_id"),
        Index("iq_handoff_ticket_no", "ticket_no", unique=True),
        Index("ix_handoff_tickets_tenant", "tenant_id"),
        Index("ix_handoff_tickets_tenant_status", "tenant_id", "status"),
    )
