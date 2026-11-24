# src/knowledge/cache.py
#
# 检索结果缓存（进程内，带 TTL）。
#
# 为什么需要：
#   同一个问题在一轮对话里会被重复检索 —— supervisor 路由到 drug_agent 时查一次文档，
#   再路由到 knowledge_agent 时又查一次，多通道融合里还会再查一次（实测同一请求
#   文档检索跑了 3 次，其中一次 30 向量 + 30 BM25 → 55 条 chunk，白等 40+ 秒）。
#   缓存命中的是"检索结果"（公共知识库 chunk / 图谱记录），不含患者数据，
#   所以可以跨请求共享；键里带 doc_type 等检索参数，避免不同类型串味。
#
# 并发语义：
#   - 不同查询并行计算，不互相排队
#   - 同一查询并发进来时只算一次，其余等同一份结果（in-flight 合并）

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Awaitable, Callable

from loguru import logger

from src.core.config import get_settings

settings = get_settings()

_CACHE: dict[str, tuple[float, object]] = {}
_INFLIGHT: dict[str, asyncio.Task] = {}
_STATS = {"hit": 0, "miss": 0}


def make_key(*parts: object) -> str:
    """把检索参数序列化成稳定键（参数顺序/类型不同会得到不同的键）。"""
    raw = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


async def cached(
    key: str,
    factory: Callable[[], Awaitable],
    *,
    ttl: float | None = None,
    label: str = "",
) -> object:
    """取缓存；没有或过期就调用 factory 计算一次。"""
    ttl = settings.KB_QUERY_CACHE_TTL_SECONDS if ttl is None else ttl
    now = time.monotonic()

    entry = _CACHE.get(key)
    if entry is not None and entry[0] > now:
        _STATS["hit"] += 1
        logger.debug(f"检索缓存命中 {label or key[:8]}")
        return entry[1]

    task = _INFLIGHT.get(key)
    if task is not None:  # 同一查询已经在算，等它
        return await asyncio.shield(task)

    task = asyncio.create_task(factory())
    _INFLIGHT[key] = task
    try:
        value = await task
    finally:
        _INFLIGHT.pop(key, None)

    _STATS["miss"] += 1
    _CACHE[key] = (time.monotonic() + ttl, value)
    _evict()
    return value


def _evict() -> None:
    limit = settings.KB_QUERY_CACHE_MAX_ENTRIES
    if len(_CACHE) <= limit:
        return
    now = time.monotonic()
    expired = [k for k, (exp, _) in _CACHE.items() if exp <= now]
    for k in expired:
        _CACHE.pop(k, None)
    if len(_CACHE) > limit:  # 还超就按到期时间淘汰最旧的
        for k, _ in sorted(_CACHE.items(), key=lambda kv: kv[1][0])[: len(_CACHE) - limit]:
            _CACHE.pop(k, None)


def stats() -> dict[str, int]:
    return {**_STATS, "entries": len(_CACHE)}


def clear() -> None:
    """知识库内容变更后调用。"""
    _CACHE.clear()
