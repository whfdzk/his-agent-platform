# src/modules/handoff/bus.py
#
# C3 人工接管：工单事件总线（工单状态 / 坐席回复的实时推送）。
#
# 位置说明：这一类"要连 Redis 才能 publish"的实现必须待在 modules 层。
# core/handoff_bus.py 早先版本在这里 import src.infra.redis_cache，被分层测试
# （test_core_layer_never_imports_upper_layers）判为反向依赖；现在只把
# 主题名这类纯常量留在 core，实现整体搬到 modules/handoff。
#
# 为什么不用 src/core/event_bus.py：那个实现按 session_id 只保留"一个"订阅者，
# 且队列满了就丢、超时就结束。工单广播需要两类受众同时在线：
#   patient:{patient_id}  —— 患者端状态条 / 会话里的坐席回复
#   seat                  —— 医护端坐席台（队列变化就刷新）
# 所以这里按"主题"维护订阅者集合，一个主题可以有多个队列，出问题也只丢事件、
# 不影响写库。
#
# 底层是 Redis Pub/Sub 而不是进程内队列：uvicorn 多 worker 或多副本部署时，
# "写库/广播"和"SSE 订阅"很可能落在不同进程，进程内队列永远收不到对端事件。
# 任一进程 publish，所有进程的订阅者都能收到；接口形状（subscribe /
# unsubscribe / publish）保持不变。
#
# 订阅侧实现：每个订阅者独占一个 pubsub 连接，后台任务把 Redis 消息转发进本地
# asyncio.Queue —— SSE 生成器仍然 await queue.get()，无需感知 Redis；
# 消费端卡住（浏览器标签冻结）时队列满就丢事件，不阻塞转发任务。

from __future__ import annotations

import asyncio
import json
from typing import Any

from loguru import logger

SEAT_TOPIC = "seat"

# Redis 频道前缀：避免与业务缓存 key 撞名，也方便 redis-cli PUBSUB CHANNELS 排查
CHANNEL_PREFIX = "handoff:bus:"


def patient_topic(patient_id: int) -> str:
    return f"patient:{int(patient_id)}"


def _channel(topic: str) -> str:
    return f"{CHANNEL_PREFIX}{topic}"


class HandoffBus:
    """按主题的广播：publish 走 Redis，subscribe 返回本地队列。"""

    def __init__(self) -> None:
        # id(queue) -> (pubsub, forward_task)
        self._subs: dict[int, tuple[Any, asyncio.Task]] = {}

    async def subscribe(self, topic: str) -> asyncio.Queue:
        from src.infra.redis_cache import get_redis_client

        queue: asyncio.Queue = asyncio.Queue(maxsize=50)
        client = await get_redis_client()
        pubsub = client.pubsub(ignore_subscribe_messages=True)
        await pubsub.subscribe(_channel(topic))
        task = asyncio.create_task(self._forward(pubsub, queue, topic), name=f"handoff-bus:{topic}")
        self._subs[id(queue)] = (pubsub, task)
        return queue

    async def unsubscribe(self, topic: str, queue: asyncio.Queue) -> None:
        entry = self._subs.pop(id(queue), None)
        if entry is None:
            return
        pubsub, task = entry
        task.cancel()
        try:
            await pubsub.unsubscribe(_channel(topic))
            await pubsub.aclose()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[handoff-bus] 关闭订阅失败 topic={topic}: {type(e).__name__}: {e}")

    async def subscriber_count(self, topic: str) -> int:
        """本进程内的订阅者数量（跨进程要看 redis PUBSUB NUMSUB）。"""
        return len(self._subs)

    async def publish(self, topic: str, event: dict[str, Any]) -> int:
        """广播一条事件，返回 Redis 报告的接收端数量。没有订阅者时返回 0。"""
        from src.infra.redis_cache import get_redis_client

        client = await get_redis_client()
        payload = json.dumps(event, ensure_ascii=False)
        receivers = await client.publish(_channel(topic), payload)
        return int(receivers or 0)

    async def _forward(self, pubsub: Any, queue: asyncio.Queue, topic: str) -> None:
        """把 Redis 消息搬到本地队列；Redis 抖动时重连，不让 SSE 直接断掉。"""
        while True:
            try:
                async for raw in pubsub.listen():
                    data = raw.get("data")
                    if data is None:
                        continue
                    try:
                        event = json.loads(data)
                    except (TypeError, ValueError):
                        logger.warning(f"[handoff-bus] 收到非 JSON 消息，已忽略 topic={topic}")
                        continue
                    try:
                        queue.put_nowait(event)
                    except asyncio.QueueFull:
                        # 消费端卡住时宁可丢这条，也不阻塞转发；
                        # 前端还有 8~10s 轮询兜底，不会永久不一致。
                        logger.warning(f"[handoff-bus] 队列满，丢弃事件 topic={topic}")
                # listen() 正常结束（被取消或连接关闭）→ 退出循环
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[handoff-bus] 订阅中断，1s 后重连 topic={topic}: {type(e).__name__}: {e}")
                await asyncio.sleep(1)
                try:
                    await pubsub.subscribe(_channel(topic))
                except Exception as e2:  # noqa: BLE001
                    logger.warning(f"[handoff-bus] 重连失败 topic={topic}: {type(e2).__name__}: {e2}")
                    await asyncio.sleep(2)


_BUS: HandoffBus | None = None


def get_handoff_bus() -> HandoffBus:
    global _BUS
    if _BUS is None:
        _BUS = HandoffBus()
    return _BUS


async def publish_ticket_event(
    ticket_snapshot: dict[str, Any], *, message: dict[str, Any] | None = None
) -> None:
    """工单变化广播：患者侧收工单快照（可带一条新消息），坐席侧只收"去刷新"信号。

    坐席台不直接吃快照：它自己有筛选/选中态，收到队列变更让它自己重拉一次
    比把快照塞进本地状态更不容易错。
    """
    bus = get_handoff_bus()
    patient_id = ticket_snapshot.get("patient_id")
    if patient_id:
        try:
            await bus.publish(
                patient_topic(int(patient_id)),
                {"type": "ticket", "ticket": ticket_snapshot},
            )
            if message:
                await bus.publish(
                    patient_topic(int(patient_id)),
                    {"type": "message", **message},
                )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[handoff-bus] 患者侧广播失败: {type(e).__name__}: {e}")
    try:
        await bus.publish(SEAT_TOPIC, {"type": "queue", "ticket_no": ticket_snapshot.get("ticket_no")})
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[handoff-bus] 坐席侧广播失败: {type(e).__name__}: {e}")
