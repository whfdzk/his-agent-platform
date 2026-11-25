# src/api/routers/admin_events.py
#
# 管理端：事件总线观测。回答"消息积压了吗、有没有死信、哪类事件在跑"。
# 数据来源是 Redis（流长 / 消费组 / Pending / 死信）+ event_consumptions（已处理明细）。

from __future__ import annotations

from fastapi import APIRouter, Depends

from src.api.deps import get_current_admin
from src.infra import event_bus
from src.modules.events import consumer

router = APIRouter(
    prefix="/api/v1/admin/events",
    tags=["admin-events"],
    dependencies=[Depends(get_current_admin)],
)


@router.get("/stats")
async def event_stats():
    return {
        "bus": await event_bus.stats(consumer.GROUP),
        "handled_by_type": await consumer.counters(),
    }


@router.get("/recent")
async def recent_events(limit: int = 20):
    items = await consumer.recent(limit)
    return {"items": items, "total": len(items)}

