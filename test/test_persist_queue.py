# 批量持久化队列的确定性单测。
# 重点：聊天消息不能被静默丢弃，统计类数据丢弃时必须留下痕迹。

import asyncio
import contextlib
import time

import pytest

from src.core import persist_queue
from src.core.persist_queue import PersistItem, enqueue, enqueue_async, get_queue


@pytest.fixture(autouse=True)
def _isolate_queue():
    """每个用例使用独立的队列与落库实现，避免用例间互相污染。"""
    persist_queue._queue = None
    previous_writer = persist_queue._writer
    persist_queue._writer = None
    yield
    persist_queue._queue = None
    persist_queue._writer = previous_writer


def _item(kind: str = "chat_message", **payload) -> PersistItem:
    return PersistItem(kind=kind, payload=payload)


async def test_enqueue_accepts_item_without_blocking():
    enqueue(_item("token_usage", model="m"))
    assert get_queue().qsize() == 1


async def test_enqueue_async_accepts_item():
    assert await enqueue_async(_item(role="user")) is True
    assert get_queue().qsize() == 1


async def test_enqueue_drops_when_full(monkeypatch):
    small: asyncio.Queue = asyncio.Queue(maxsize=1)
    await small.put(_item("token_usage"))
    monkeypatch.setattr(persist_queue, "get_queue", lambda: small)

    enqueue(_item("token_usage"))  # 统计类数据：不抛异常，直接丢弃
    assert small.qsize() == 1


async def test_enqueue_async_gives_up_after_bounded_wait(monkeypatch):
    small: asyncio.Queue = asyncio.Queue(maxsize=1)
    await small.put(_item("chat_message"))
    monkeypatch.setattr(persist_queue, "get_queue", lambda: small)

    assert await enqueue_async(_item("chat_message"), timeout=0.01) is False
    assert small.qsize() == 1  # 超时后未挤掉已有数据


async def test_enqueue_async_succeeds_once_space_frees_up(monkeypatch):
    small: asyncio.Queue = asyncio.Queue(maxsize=1)
    await small.put(_item("chat_message"))
    monkeypatch.setattr(persist_queue, "get_queue", lambda: small)

    async def _drain():
        await asyncio.sleep(0.02)
        await small.get()

    drain_task = asyncio.create_task(_drain())
    assert await enqueue_async(_item("chat_message"), timeout=0.5) is True
    await drain_task


async def test_write_batch_delegates_to_injected_writer():
    seen: list[int] = []

    async def _fake_writer(batch: list[PersistItem]) -> None:
        seen.append(len(batch))

    persist_queue.set_writer(_fake_writer)
    await persist_queue._write_batch([_item(), _item()])
    assert seen == [2]


async def test_write_batch_without_writer_does_not_raise():
    # core 不依赖 modules，未注入落库实现时宁可丢弃也不能崩
    assert await persist_queue._write_batch([_item()]) is None


async def test_consumer_requeues_batch_instead_of_dropping(monkeypatch):
    """写入失败时批次要重新入队，而不是随 finally 一起清空。"""
    written: list[list[PersistItem]] = []
    state = {"fail_next": True}

    async def _flaky(batch: list[PersistItem]) -> None:
        if state["fail_next"]:
            state["fail_next"] = False
            raise RuntimeError("db down")
        written.append(list(batch))

    monkeypatch.setattr(persist_queue, "_write_batch", _flaky)
    monkeypatch.setattr(persist_queue, "FLUSH_INTERVAL", 0.01)

    consumer = asyncio.create_task(persist_queue._consumer())
    try:
        await get_queue().put(_item(role="user", content="你好"))

        deadline = time.monotonic() + 2.0
        while not written and time.monotonic() < deadline:
            await asyncio.sleep(0.01)

        assert [[i.payload["content"] for i in batch] for batch in written] == [["你好"]]
    finally:
        consumer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await consumer
