# src/utils/token_revocation.py
#
# JWT 登出 / 撤销支持。
#
# 无状态 JWT 无法"删除"，登出只能由服务端记住"这份凭证已作废"：
# - 单端登出：把 token 的 jti 写入 Redis 黑名单，TTL = token 剩余有效期
#   （过期即自动清理，不会随登出次数线性增长）
# - 全部设备登出：按 (scope, 用户ID) 记录一个"失效时间点"，
#   签发时间早于该时间点的 token 一律拒绝（时间精度为秒级）
#
# 可用性取舍：Redis 不可用时这里返回 None（表示"判断不了"），
# 由调用方决定 fail-open（患者端，可用性优先）还是 fail-closed
# （管理端，安全优先，见 AUTH_ADMIN_FAIL_CLOSED）。
# 同时带一个短期熔断：连续失败后 15s 内直接跳过，避免每个请求都等超时。

from __future__ import annotations

import time
from typing import Any

from loguru import logger

from src.core.config import get_settings

# 撤销名单 key 前缀：与业务缓存区分，便于 Redis 侧按前缀排查/清理
JTI_KEY_PREFIX = "auth:revoked:jti:"
CUTOFF_KEY_PREFIX = "auth:revoked:at:"

# Redis 故障后的熔断时长（秒）：期间不再尝试连接，直接按"不可用"处理
_UNAVAILABLE_COOLDOWN_SECONDS = 15.0

# 独立的短超时连接池：鉴权链路不能被 Redis 慢请求拖住
_POOL_KWARGS = dict(
    max_connections=50,
    socket_timeout=0.5,
    socket_connect_timeout=0.5,
    health_check_interval=30,
)

_client: Any = None
_unavailable_until: float = 0.0


def _get_client() -> Any:
    """延迟导入并创建客户端：模块导入本身不依赖 redis 包/连接。"""
    global _client
    if _client is None:
        import redis.asyncio as redis

        settings = get_settings()
        _client = redis.Redis(
            host=settings.REDIS_HOST,
            port=settings.REDIS_PORT,
            db=settings.REDIS_DB,
            password=settings.REDIS_PASSWORD or None,
            decode_responses=True,
            **_POOL_KWARGS,
        )
    return _client


def _in_cooldown() -> bool:
    return time.monotonic() < _unavailable_until


def _mark_unavailable(exc: Exception) -> None:
    global _unavailable_until
    _unavailable_until = time.monotonic() + _UNAVAILABLE_COOLDOWN_SECONDS
    logger.warning(
        f"JWT 撤销存储不可用，{_UNAVAILABLE_COOLDOWN_SECONDS:.0f}s 内跳过撤销校验: "
        f"{type(exc).__name__}"
    )


def reset_state() -> None:
    """重置连接与熔断状态（测试用）。"""
    global _client, _unavailable_until
    _client = None
    _unavailable_until = 0.0


def _jti_key(jti: str) -> str:
    return f"{JTI_KEY_PREFIX}{jti}"


def _cutoff_key(scope: str, subject: str) -> str:
    return f"{CUTOFF_KEY_PREFIX}{scope}:{subject}"


async def revoke_jti(jti: str, ttl_seconds: int) -> bool:
    """把单个 token 的 jti 加入撤销名单。返回是否写入成功。"""
    if not jti or ttl_seconds <= 0:
        return False
    try:
        await _get_client().set(_jti_key(jti), "1", ex=ttl_seconds)
        return True
    except Exception as e:  # Redis 故障：登出接口不因此失败，由前端本地清理兜底
        _mark_unavailable(e)
        return False


async def is_jti_revoked(jti: str) -> bool | None:
    """token 是否已被撤销；Redis 不可用返回 None。"""
    if not jti:
        return False
    if _in_cooldown():
        return None
    try:
        return bool(await _get_client().exists(_jti_key(jti)))
    except Exception as e:
        _mark_unavailable(e)
        return None


async def revoke_session_before(scope: str, subject: str, cutoff: float) -> bool:
    """让该账号在 cutoff 之前签发的全部 token 失效（登出全部设备）。

    只保留更晚的 cutoff：重复登出不会把已重新登录的会话再踢一次。
    """
    if not subject:
        return False
    ttl = int(getattr(get_settings(), "AUTH_REVOCATION_TTL_DAYS", 90)) * 86400
    try:
        client = _get_client()
        key = _cutoff_key(scope, subject)
        current = await client.get(key)
        if current is not None:
            try:
                cutoff = max(cutoff, float(current))
            except (TypeError, ValueError):
                pass
        await client.set(key, f"{cutoff:.6f}", ex=max(ttl, 60))
        return True
    except Exception as e:
        _mark_unavailable(e)
        return False


async def is_session_revoked(scope: str, subject: str, issued_at: float | None) -> bool | None:
    """签发时间早于账号失效时间点 → 已撤销；Redis 不可用返回 None。"""
    if not subject or issued_at is None:
        return False
    if _in_cooldown():
        return None
    try:
        raw = await _get_client().get(_cutoff_key(scope, subject))
    except Exception as e:
        _mark_unavailable(e)
        return None
    if raw is None:
        return False
    try:
        return issued_at <= float(raw)
    except (TypeError, ValueError):
        return False
