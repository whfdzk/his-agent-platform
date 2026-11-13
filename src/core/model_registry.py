# src/core/model_registry.py
#
# 候选模型注册表：LLM 故障转移用的「模型链」。
#
# 分层约束：core 是最底层，不能 import modules / infra（见 test_layering.py），
# 而候选模型存在 model_configs 表里。所以这里只定义数据结构与缓存，
# 真正的读库实现由 modules 层通过 set_provider 注入（与 persist_queue 同一套路）。

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from loguru import logger

# 候选链缓存多久（秒）。管理端改完配置主动刷新；这里只兜住"另一个副本改了库"的情况。
DEFAULT_REFRESH_INTERVAL = 60.0


@dataclass(frozen=True)
class ModelCandidate:
    """一个可用的聊天模型。base_url / api_key 为空时回退到 .env 的凭证。"""

    name: str
    model_name: str
    base_url: str = ""
    api_key: str = ""


Provider = Callable[[], Awaitable[list[ModelCandidate]]]

_provider: Provider | None = None
_cache: list[ModelCandidate] = []
_cached_at = 0.0
# 每次刷新自增：用于日志与排查"为什么还在用旧模型"
_version = 0


def set_provider(provider: Provider) -> None:
    """注入读库实现（modules 层负责，见 src/modules/ops/model_provider.py）。"""
    global _provider
    _provider = provider


def version() -> int:
    return _version


def cached_candidates() -> list[ModelCandidate]:
    """同步取当前缓存，供无法 await 的调用路径（如 _generate）使用。"""
    return _cache


def cache_age() -> float:
    return 0.0 if _cached_at == 0.0 else time.monotonic() - _cached_at


async def refresh_candidates() -> list[ModelCandidate]:
    """强制刷新候选链（启动、管理端保存配置后调用）。"""
    global _cache, _cached_at, _version
    if _provider is None:
        return _cache
    try:
        candidates = await _provider()
    except Exception as e:
        # 读库失败不能让整轮对话挂掉：保留上一次的链继续服务
        logger.warning(f"候选模型刷新失败，继续使用上次的链: {type(e).__name__}: {e}")
        return _cache
    if candidates != _cache:
        logger.info(
            "候选模型链已更新 v{}: {}",
            _version + 1,
            [f"{c.name}({c.model_name})" for c in candidates],
        )
    _cache = candidates
    _cached_at = time.monotonic()
    _version += 1
    return _cache


async def get_candidates(
    refresh_interval: float = DEFAULT_REFRESH_INTERVAL,
) -> list[ModelCandidate]:
    """取候选链；超过刷新间隔就顺带刷新一次（异步路径用这个）。"""
    if _provider is None:
        return _cache
    if not _cache or cache_age() >= refresh_interval:
        await refresh_candidates()
    return _cache


def reset() -> None:
    """仅测试用：清空缓存与注入的实现。"""
    global _provider, _cache, _cached_at, _version
    _provider = None
    _cache = []
    _cached_at = 0.0
    _version = 0
