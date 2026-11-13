# src/core/persist_queue.py
#
# 进程内异步批量持久化队列。
#
# 背景：10k 并发下，"每次 LLM 调用 / 每条消息各新建一个 DB session 并 commit"
# 会以每秒数万次的频率打爆连接池（pool_size=30+20）。
#
# 方案：单一 consumer task 消费 asyncio.Queue，攒批（50 条或 500ms）后用
# 少量长连接批量写入；单行失败重试 3 次后仅记日志（与原行为一致：统计类
# 数据不阻断主流程）。应用关闭时 flush 余量。

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

BATCH_SIZE = 50
FLUSH_INTERVAL = 0.5  # 秒
MAX_RETRIES = 3
_QUEUE_MAX = 20000  # 背压上限
# 队列满时最多等待多久：宁可短暂等待，也不要静默丢弃聊天消息
ENQUEUE_WAIT_TIMEOUT = 0.5  # 秒


@dataclass
class PersistItem:
    kind: str  # "chat_message" | "token_usage"
    payload: dict[str, Any] = field(default_factory=dict)


_queue: asyncio.Queue[PersistItem] | None = None
_consumer_task: asyncio.Task | None = None
_dropped = 0
# 落库实现由 modules 层注入（依赖倒置，见 set_writer）
_writer: Callable[[list[PersistItem]], Awaitable[None]] | None = None


def set_writer(writer: Callable[[list[PersistItem]], Awaitable[None]]) -> None:
    """注入落库实现。

    队列只负责排队与攒批，具体 SQL 属于 modules 层；靠注入而不是直接 import，
    core 才不会反向依赖 infra / modules。
    """
    global _writer
    _writer = writer


def get_queue() -> asyncio.Queue[PersistItem]:
    global _queue
    if _queue is None:
        _queue = asyncio.Queue(maxsize=_QUEUE_MAX)
    return _queue


def _note_drop(kind: str) -> None:
    global _dropped
    _dropped += 1
    if _dropped % 100 == 1:
        logger.error(f"persist_queue 已满，累计丢弃 {_dropped} 条（最近一条 kind={kind}）")


def enqueue(item: PersistItem) -> None:
    """非阻塞投递；队列满说明 DB 已严重过载，丢弃并告警（保护主链路）。

    仅用于统计类数据（token_usage）。聊天消息请用 enqueue_async，
    它会在队列满时有界等待，避免用户对话静默丢失。
    """
    q = get_queue()
    try:
        q.put_nowait(item)
    except asyncio.QueueFull:
        _note_drop(item.kind)


async def enqueue_async(item: PersistItem, timeout: float = ENQUEUE_WAIT_TIMEOUT) -> bool:
    """有界等待地投递，供聊天消息使用；等待超时才丢弃，并返回 False。

    等待时长有上限，保证 DB 故障时聊天主链路不会被无限阻塞。
    """
    q = get_queue()
    try:
        await asyncio.wait_for(q.put(item), timeout=timeout)
        return True
    except (asyncio.TimeoutError, TimeoutError):
        _note_drop(item.kind)
        logger.error(f"persist_queue 等待 {timeout}s 仍满，丢弃 1 条 {item.kind}")
        return False


async def _write_batch(batch: list[PersistItem]) -> None:
    """落库一个批次；具体写入由 modules 层注入（见 set_writer）。"""
    if _writer is None:
        logger.error(f"persist_queue 未注入落库实现，丢弃 {len(batch)} 条")
        return
    await _writer(batch)


async def _requeue(batch: list[PersistItem], attempt: int) -> None:
    """把未落库的批次投回队列，带指数退避。

    此前用非阻塞 enqueue 立即重投：DB 持续故障时会形成紧凑重试循环，
    且队列满时直接丢弃。这里改为先退避再走有界等待投递。
    """
    if not batch:
        return
    # 首次 0.5s，之后翻倍，上限 10s：避免 DB 抖动时形成紧凑重试循环，
    # 也不能退避太久导致关闭流程排空变慢
    delay = min(0.5 * 2 ** min(attempt - 1, 5), 10.0)
    logger.warning(f"persist_queue 回投 {len(batch)} 条，{delay:.1f}s 后重试")
    await asyncio.sleep(delay)
    for item in batch:
        await enqueue_async(item)


async def _consumer() -> None:
    batch: list[PersistItem] = []
    q = get_queue()
    loop = asyncio.get_running_loop()
    attempt = 0
    while True:
        try:
            # 攒批：先等第一条，再非阻塞收满 BATCH_SIZE 或到 flush 间隔
            item = await q.get()
            batch.append(item)
            deadline = loop.time() + FLUSH_INTERVAL
            while len(batch) < BATCH_SIZE:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                try:
                    item = await asyncio.wait_for(q.get(), timeout=remaining)
                    batch.append(item)
                except asyncio.TimeoutError:
                    break
            await _write_batch(batch)
            attempt = 0
        except asyncio.CancelledError:
            if batch:
                # shield：关闭途中的第二次取消不能打断最后的落库
                try:
                    await asyncio.shield(_write_batch(batch))
                except asyncio.CancelledError:
                    logger.error(f"persist_queue 关闭前 flush 被打断，丢弃 {len(batch)} 条")
                except Exception as e:
                    logger.error(f"persist_queue 关闭前 flush 失败，丢弃 {len(batch)} 条: {e}")
            raise
        except Exception as e:
            logger.error(f"persist_queue consumer 异常: {e}")
            # 异常路径不清空批次：退避后重新入队，避免静默丢失
            attempt += 1
            if batch:
                await _requeue(batch, attempt)
        finally:
            batch = []


def start_consumer() -> None:
    """lifespan 启动时调用。"""
    global _consumer_task
    if _consumer_task is None or _consumer_task.done():
        _consumer_task = asyncio.create_task(_consumer())
        logger.info("persist_queue consumer 已启动")


async def stop_consumer() -> None:
    """lifespan 关闭时调用：先 flush 当前批次，再把队列余量全部落库。

    此前的实现只 cancel 一次：cancel 只保证"已取出的批次"被 flush，
    队列里尚未取出的条目随着进程退出一起消失（滚动发布时丢消息）。
    """
    global _consumer_task
    task = _consumer_task
    if task is None:
        return

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    _consumer_task = None

    await _drain()


async def _drain() -> None:
    """排空队列余量。失败只记日志：关闭流程不能因 DB 不可用而卡死。"""
    q = get_queue()
    drained = 0
    try:
        while not q.empty():
            batch: list[PersistItem] = []
            while len(batch) < BATCH_SIZE and not q.empty():
                try:
                    batch.append(q.get_nowait())
                except asyncio.QueueEmpty:
                    break
            if not batch:
                break
            await _write_batch(batch)
            drained += len(batch)
    except Exception as e:
        logger.error(f"persist_queue 关闭前排空失败（剩余 {q.qsize()} 条）: {e}")
    if drained:
        logger.info(f"persist_queue 关闭前已完成排空 {drained} 条")
