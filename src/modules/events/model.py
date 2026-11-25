# src/modules/events/model.py
#
# 事件消费流水表：一条事件被某个消费者处理成功后落一行。
# (event_id, consumer) 唯一 —— 这就是消费端幂等的落地点：
# Redis 至少一次投递，重复投递到同一个消费者只会命中唯一约束，业务只执行一次。

from sqlalchemy import BigInteger, DateTime, Index, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.core.base_model import BaseModel


class EventConsumption(BaseModel):
    __tablename__ = "event_consumptions"

    event_id: Mapped[str] = mapped_column(String(200), nullable=False, comment="事件 ID（type:key）")
    consumer: Mapped[str] = mapped_column(String(64), nullable=False, comment="消费组名")
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, comment="事件类型")
    tenant_id: Mapped[int] = mapped_column(BigInteger, default=1, comment="租户")
    status: Mapped[str] = mapped_column(String(16), default="done", comment="done / failed")
    attempts: Mapped[int] = mapped_column(Integer, default=1, comment="第几次投递处理")
    error: Mapped[str] = mapped_column(String(500), default="", comment="失败原因")
    payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True, comment="事件负载快照")
    handled_at: Mapped[object] = mapped_column(DateTime, nullable=True, comment="处理成功时间")

    __table_args__ = (
        Index("uq_event_event_consumer", "event_id", "consumer", unique=True),
        Index("ix_event_consumptions_type", "event_type"),
    )
