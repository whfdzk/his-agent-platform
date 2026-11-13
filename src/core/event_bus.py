"""
事件总线：Agent 执行过程中实时推送事件到前端。
支持：思考中 / 调用工具 / 生成回答 / 完成 / 错误等事件类型。
"""
from __future__ import annotations
import asyncio
from typing import AsyncGenerator, Any
from loguru import logger
from collections import defaultdict


class EventBus:
    """简单的事件总线，按 session_id 订阅事件。"""

    def __init__(self):
        self._subscribers: dict[str, asyncio.Queue] = {}
        self._lock = asyncio.Lock()

    async def subscribe(self, session_id: str) -> asyncio.Queue:
        """订阅某个会话的事件，返回一个队列。"""
        async with self._lock:
            queue = asyncio.Queue(maxsize=100)
            self._subscribers[session_id] = queue
            logger.info(f"[EventBus] 订阅会话: {session_id}")
            return queue

    async def unsubscribe(self, session_id: str) -> None:
        """取消订阅。"""
        async with self._lock:
            if session_id in self._subscribers:
                del self._subscribers[session_id]
                logger.info(f"[EventBus] 取消订阅: {session_id}")

    async def publish(self, session_id: str, event_type: str, data: Any = None) -> None:
        """发布事件到指定会话。"""
        if session_id not in self._subscribers:
            return

        event = {
            "type": event_type,
            "data": data or {},
        }

        try:
            queue = self._subscribers[session_id]
            queue.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning(f"[EventBus] 队列满，丢弃事件: {session_id} {event_type}")
        except Exception as e:
            logger.error(f"[EventBus] 发布事件失败: {e}")

    async def stream_events(self, session_id: str) -> AsyncGenerator[dict, None]:
        """流式获取事件，直到收到 done 事件。"""
        queue = await self.subscribe(session_id)
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=30.0)
                    yield event
                    if event["type"] == "done":
                        break
                except asyncio.TimeoutError:
                    # 超时发送心跳
                    yield {"type": "ping", "data": {}}
        finally:
            await self.unsubscribe(session_id)


# 全局事件总线实例
_global_event_bus = None


def get_event_bus() -> EventBus:
    """获取全局事件总线单例。"""
    global _global_event_bus
    if _global_event_bus is None:
        _global_event_bus = EventBus()
    return _global_event_bus


# 便捷函数
async def emit_thinking(session_id: str, content: str) -> None:
    """推送思考中事件。"""
    await get_event_bus().publish(session_id, "thinking", {"content": content})


async def emit_tool_call(session_id: str, tool_name: str, args: dict) -> None:
    """推送工具调用事件。"""
    await get_event_bus().publish(session_id, "tool_call", {
        "tool_name": tool_name,
        "args": args,
    })


async def emit_tool_result(session_id: str, tool_name: str, result: str) -> None:
    """推送工具调用结果事件。"""
    await get_event_bus().publish(session_id, "tool_result", {
        "tool_name": tool_name,
        "result": result[:500],  # 截断，避免太大
    })


async def emit_token(session_id: str, token: str) -> None:
    """推送生成的 token 事件。"""
    await get_event_bus().publish(session_id, "token", {"token": token})


async def emit_done(session_id: str, answer: str = "") -> None:
    """推送完成事件。"""
    await get_event_bus().publish(session_id, "done", {"answer": answer})


async def emit_error(session_id: str, error: str) -> None:
    """推送错误事件。"""
    await get_event_bus().publish(session_id, "error", {"error": error})
