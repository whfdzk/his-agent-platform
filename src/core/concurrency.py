# src/core/concurrency.py
"""全局并发控制：LLM 信号量 + 超时包装 + SSE 连接计数。

10k 在线用户下必须对上游 LLM 调用做全局限流与超时保护，
避免上游变慢时连接无限堆积导致雪崩。
"""
from __future__ import annotations

import asyncio
import contextvars
import time
from collections.abc import Callable
from functools import partial
from typing import Any, AsyncIterator, Awaitable, TypeVar

import anyio
from loguru import logger

from src.core.config import get_settings

T = TypeVar("T")


async def to_thread(func: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    """把同步阻塞调用挪到线程池执行，避免占住 event loop。

    必须走这里的同步 IO：
    - pymilvus 的 Collection/connections API（gRPC）
    - boto3 的 S3 客户端（RustFS）
    - LlamaIndex 的文档加载与切分（CPU 密集 + 磁盘 IO）

    在 async 函数里直接调用它们会让整个 worker 进程的所有请求停摆。
    anyio 默认线程池上限 40，IO 密集型任务不会把它打满。
    """
    if kwargs:
        return await anyio.to_thread.run_sync(partial(func, *args, **kwargs))
    return await anyio.to_thread.run_sync(func, *args)


# ── 全局 LLM 并发信号量（进程内） ─────────────────────────────────────────
# 单 worker 并发上限，2 worker × 200 = 400，与 httpx max_connections 对齐。
# 在模块导入期创建：懒初始化没有同步原语保护，并发首启可能建出两个 Semaphore，
# 使实际并发上限翻倍（Python 3.10+ 的 Semaphore 与事件循环解耦，可安全提前创建）。
_llm_semaphore: asyncio.Semaphore = asyncio.Semaphore(get_settings().LLM_MAX_CONCURRENCY)
_llm_in_flight = 0  # 在途 LLM 请求数（监控指标）


def get_llm_semaphore() -> asyncio.Semaphore:
    """返回进程级 LLM 信号量（已在导入期初始化，保证全进程唯一）。"""
    return _llm_semaphore


def llm_in_flight() -> int:
    return _llm_in_flight


# 等待信号量的最长时间，超时视为系统繁忙
SEMAPHORE_WAIT_TIMEOUT = 15.0


class SystemBusyError(Exception):
    """LLM 并发已满，请求应在稍后重试（映射为 503 / SSE busy 事件）。"""


async def _acquire_sem(sem: asyncio.Semaphore):
    await sem.acquire()
    return sem


async def guarded(coro: Awaitable[T], timeout: float | None = None) -> T:
    """带全局信号量与超时包装的协程执行器。

    - 先限时等待信号量（默认 15s），拿不到即抛 SystemBusyError（快速失败，不堆积）；
    - 再限时执行协程（默认取 settings.TURN_TIMEOUT）；
    - 超时/异常时确保信号量释放。
    """
    settings = get_settings()
    timeout = timeout if timeout is not None else settings.TURN_TIMEOUT
    sem = get_llm_semaphore()

    start = time.monotonic()
    try:
        acquired = await asyncio.wait_for(_acquire_sem(sem), timeout=SEMAPHORE_WAIT_TIMEOUT)
    except asyncio.TimeoutError:
        raise SystemBusyError("LLM 并发已满") from None
    waited = time.monotonic() - start
    if waited > 1.0:
        logger.warning(f"LLM 信号量排队 {waited:.1f}s")
    global _llm_in_flight
    _llm_in_flight += 1
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    finally:
        _llm_in_flight -= 1
        sem.release()


async def guarded_stream(
    agen: AsyncIterator[T], timeout: float | None = None,
) -> AsyncIterator[T]:
    """流式版 guarded：整个 astream 生命周期内持有信号量并限制总耗时。

    guarded 保护的是"一次 await"，而 SSE 主链路逐 chunk 消费 astream，
    此前完全没有并发保护，会绕过 LLM_MAX_CONCURRENCY 直接打爆上游网关。
    这里让信号量从首 token 持有到末 token，超时按总耗时计算（不是单 chunk）。
    """
    settings = get_settings()
    timeout = timeout if timeout is not None else settings.TURN_TIMEOUT
    sem = get_llm_semaphore()

    try:
        await asyncio.wait_for(_acquire_sem(sem), timeout=SEMAPHORE_WAIT_TIMEOUT)
    except asyncio.TimeoutError:
        raise SystemBusyError("LLM 并发已满") from None

    global _llm_in_flight
    _llm_in_flight += 1
    deadline = time.monotonic() + timeout
    try:
        iterator = agen.__aiter__()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                await _close_stream(agen)
                raise TimeoutError(f"LLM 流式响应超过 {timeout}s 仍未结束")
            try:
                item = await asyncio.wait_for(iterator.__anext__(), timeout=remaining)
            except asyncio.TimeoutError:
                # wait_for 会把 CancelledError 丢进 Pregel 的 asyncio.wait，
                # 底层 astream 就此停在半路（langgraph 内部 task/queue 悬挂）。
                # 必须显式 aclose 回收，否则连接与任务要等到 GC 才释放。
                await _close_stream(agen)
                raise TimeoutError(f"LLM 流式响应超过 {timeout}s 仍未结束") from None
            except StopAsyncIteration:
                break
            yield item
    finally:
        _llm_in_flight -= 1
        sem.release()


async def _close_stream(agen: AsyncIterator[Any]) -> None:
    """尽力关闭被中途取消的 astream，失败不影响已产生的 TimeoutError。"""
    aclose = getattr(agen, "aclose", None)
    if aclose is None:
        return
    try:
        await aclose()
    except Exception as e:
        logger.debug(f"astream 关闭失败（可忽略）: {e}")


# ── SSE 连接计数（进程内；多副本时由 Nginx limit_conn 兜底） ────────────────

class ConnectionLimiter:
    """进程内 SSE 并发连接计数器，超过上限抛 SystemBusyError。"""

    def __init__(self) -> None:
        self._count = 0
        self._lock = asyncio.Lock()

    @property
    def active(self) -> int:
        return self._count

    async def enter(self, max_conns: int) -> None:
        async with self._lock:
            if self._count >= max_conns:
                raise SystemBusyError("SSE 连接数已达上限")
            self._count += 1

    async def exit(self) -> None:
        async with self._lock:
            self._count = max(0, self._count - 1)


sse_limiter = ConnectionLimiter()

# ── 请求级 request_id（日志贯穿用，P2 阶段完善输出） ────────────────────────
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="")
