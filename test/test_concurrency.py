# 全局并发控制的确定性单测。
# guarded 是保护上游 LLM 的唯一闸门，信号量泄漏会直接拖垮整机吞吐。

import asyncio
import threading
import time

import pytest

from src.core import concurrency
from src.core.concurrency import (
    ConnectionLimiter,
    SystemBusyError,
    guarded,
    guarded_stream,
    to_thread,
)


async def _ticks(count: int, delay: float = 0):
    """模拟 LLM 的 token 流。"""
    for i in range(count):
        if delay:
            await asyncio.sleep(delay)
        yield i


async def test_guarded_passes_through_result():
    async def _coro():
        return "ok"

    assert await guarded(_coro()) == "ok"


async def test_guarded_releases_semaphore_on_success():
    async def _coro():
        return "ok"

    await guarded(_coro())
    assert concurrency.llm_in_flight() == 0


async def test_guarded_releases_semaphore_on_failure():
    async def _boom():
        raise RuntimeError("llm down")

    with pytest.raises(RuntimeError):
        await guarded(_boom())
    assert concurrency.llm_in_flight() == 0


async def test_guarded_enforces_timeout():
    with pytest.raises(asyncio.TimeoutError):
        await guarded(asyncio.sleep(0.5), timeout=0.01)
    assert concurrency.llm_in_flight() == 0


async def test_guarded_raises_system_busy_when_saturated(monkeypatch):
    monkeypatch.setattr(concurrency, "get_llm_semaphore", lambda: asyncio.Semaphore(0))
    monkeypatch.setattr(concurrency, "SEMAPHORE_WAIT_TIMEOUT", 0.01)

    coro = asyncio.sleep(0)
    with pytest.raises(SystemBusyError):
        await guarded(coro)
    coro.close()  # 拿不到信号量时协程从未被 await，需显式关闭避免 RuntimeWarning


async def test_to_thread_moves_blocking_call_off_the_event_loop():
    loop_thread = threading.get_ident()
    seen: dict[str, int] = {}

    def _blocking() -> str:
        seen["thread"] = threading.get_ident()
        time.sleep(0.05)
        return "ok"

    assert await to_thread(_blocking) == "ok"
    assert seen["thread"] != loop_thread  # 同步 IO 没在主线程上执行


async def test_to_thread_supports_keyword_arguments():
    assert await to_thread(dict, a=1, b=2) == {"a": 1, "b": 2}


async def test_to_thread_propagates_exceptions():
    def _boom() -> None:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await to_thread(_boom)


async def test_guarded_stream_yields_every_chunk():
    chunks = [chunk async for chunk in guarded_stream(_ticks(3))]
    assert chunks == [0, 1, 2]


async def test_guarded_stream_releases_semaphore():
    async for _ in guarded_stream(_ticks(2)):
        pass
    assert concurrency.llm_in_flight() == 0


async def test_guarded_stream_limits_total_time_not_per_chunk():
    # 单 chunk 都在 0.05s 内返回，但累计超过 0.1s 就必须中断
    with pytest.raises(TimeoutError):
        [chunk async for chunk in guarded_stream(_ticks(5, delay=0.05), timeout=0.1)]
    assert concurrency.llm_in_flight() == 0


async def test_guarded_stream_closes_source_generator_on_timeout():
    """超时后必须显式关闭底层 astream。

    wait_for 把 CancelledError 丢进 Pregel 的 asyncio.wait 后，langgraph 内部的
    task/queue 会悬挂；不 aclose 就只能等 GC 回收。
    """
    closed = False

    async def _stalled():
        nonlocal closed
        try:
            await asyncio.sleep(1)
            yield "late"
        finally:
            closed = True

    with pytest.raises(TimeoutError):
        [chunk async for chunk in guarded_stream(_stalled(), timeout=0.05)]
    assert closed
    assert concurrency.llm_in_flight() == 0


async def test_guarded_stream_raises_system_busy_when_saturated(monkeypatch):
    monkeypatch.setattr(concurrency, "get_llm_semaphore", lambda: asyncio.Semaphore(0))
    monkeypatch.setattr(concurrency, "SEMAPHORE_WAIT_TIMEOUT", 0.01)

    stream = guarded_stream(_ticks(3))
    with pytest.raises(SystemBusyError):
        await stream.__anext__()
    await stream.aclose()


async def test_connection_limiter_rejects_overflow():
    limiter = ConnectionLimiter()
    await limiter.enter(1)
    assert limiter.active == 1
    with pytest.raises(SystemBusyError):
        await limiter.enter(1)
    await limiter.exit()
    assert limiter.active == 0


async def test_connection_limiter_never_goes_negative():
    limiter = ConnectionLimiter()
    await limiter.exit()
    assert limiter.active == 0
