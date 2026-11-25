# src/core/../modules/events/consumer.py
#
# 事件消费者：一个消费组一个后台任务，至少一次投递 + 手动 ACK + 幂等落库。
#
# 关键设计：
# 1. 幂等：落 event_consumptions 表，(event_id, consumer) 唯一。
#    重复投递（重投 / 多实例接管）第二次插入命中唯一约束 → 只 ACK，不再执行业务。
# 2. 失败重试：异常时不 ACK，消息留在 Pending；循环开头 XAUTOCLAIM 接管
#    idle 超过阈值（默认 30s）的 Pending，投递次数到达上限后转死信并 ACK。
# 3. 观测：处理数与失败数进日志 + 管理端 /admin/events/stats 直接读 Redis 的
#    流长 / Pending / lag，不另建指标表。

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.infra import event_bus
from src.modules.events.model import EventConsumption

GROUP = "his-events-core"
CONSUMER = "api-inprocess-1"
MAX_DELIVERIES = 3
CLAIM_MIN_IDLE_MS = 30_000

_task: asyncio.Task | None = None
_stopping = False


async def start() -> None:
    global _task, _stopping
    if _task is not None and not _task.done():
        return
    _stopping = False
    _task = asyncio.create_task(_loop(), name="event-consumer")
    logger.info(f"事件消费者已启动 group={GROUP} consumer={CONSUMER}")


async def stop() -> None:
    global _task, _stopping
    task = _task
    if task is None:
        return
    _stopping = True
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    _task = None
    logger.info("事件消费者已停止")


async def _loop() -> None:
    try:
        await event_bus.ensure_group(GROUP)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"事件消费组初始化失败，消费任务退出: {type(e).__name__}: {e}")
        return

    while not _stopping:
        try:
            # 先接管超时未 ACK 的消息（上一个实例崩了 / 本实例处理超时）
            stale = await event_bus.claim_stale(GROUP, CONSUMER, min_idle_ms=CLAIM_MIN_IDLE_MS)
            for msg_id, fields in stale:
                await _handle(msg_id, fields)
            batch = await event_bus.read_group(GROUP, CONSUMER, block_ms=2000)
            for msg_id, fields in batch:
                await _handle(msg_id, fields)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            # 消费循环自身异常（Redis 抖动等）：退避后继续，不能整组退出
            logger.warning(f"事件消费循环异常: {type(e).__name__}: {e}")
            await asyncio.sleep(2)


async def _handle(msg_id: str, fields: dict) -> None:
    event_id = str(fields.get("id") or "")
    event_type = str(fields.get("type") or "")
    if not event_id or not event_type:
        await event_bus.ack(GROUP, msg_id)
        return

    try:
        payload = json.loads(fields.get("payload") or "{}")
    except Exception:  # noqa: BLE001
        payload = {}

    try:
        handled = await _record(
            event_id=event_id,
            event_type=event_type,
            tenant_id=int(fields.get("tenant_id") or 1),
            payload=payload,
        )
    except Exception as e:  # noqa: BLE001
        n = await event_bus.deliveries(GROUP, msg_id)
        logger.error(f"事件处理失败 {event_id} 第 {n} 次: {type(e).__name__}: {e}")
        if n >= MAX_DELIVERIES:
            await event_bus.to_dead_letter(GROUP, msg_id, fields, f"{type(e).__name__}: {e}")
        # 未到上限：留在 Pending，等下一轮 XAUTOCLAIM 重投
        return

    await event_bus.ack(GROUP, msg_id)
    if handled:  # handled=False 表示重复投递，幂等命中
        logger.info(f"事件处理完成 {event_id}")


async def _record(
    *, event_id: str, event_type: str, tenant_id: int, payload: dict[str, Any],
) -> bool:
    """写消费流水；返回 True=首次处理，False=重复投递（幂等命中）。"""
    from src.infra.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        stmt = (
            pg_insert(EventConsumption)
            .values(
                event_id=event_id,
                consumer=GROUP,
                event_type=event_type,
                tenant_id=tenant_id,
                status="done",
                attempts=1,
                payload=payload,
                handled_at=datetime.now(),
            )
            .on_conflict_do_nothing(index_elements=["event_id", "consumer"])
        )
        result = await db.execute(stmt)
        await db.commit()
        # on_conflict_do_nothing 命中时返回 rowcount=0
        return bool(result.rowcount)


async def recent(limit: int = 20) -> list[dict]:
    """管理端：最近处理过的事件（新的在前）。"""
    from src.infra.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(EventConsumption)
                .order_by(EventConsumption.id.desc())
                .limit(max(1, min(limit, 200)))
            )
        ).scalars().all()
    return [
        {
            "event_id": r.event_id,
            "type": r.event_type,
            "tenant_id": r.tenant_id,
            "status": r.status,
            "handled_at": r.handled_at.isoformat() if r.handled_at else None,
        }
        for r in rows
    ]


async def counters() -> dict:
    """按事件类型统计已处理条数（管理端展示用）。"""
    from src.infra.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(EventConsumption.event_type, func.count(EventConsumption.id))
                .group_by(EventConsumption.event_type)
            )
        ).all()
    return {str(t): int(n) for t, n in rows}

