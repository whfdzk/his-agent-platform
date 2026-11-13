# src/middlewares/rate_limit.py
#
# 基于 Redis 滑动窗口的接口限流（依赖注入形式，挂在具体端点上而非全局中间件，
# 避免对 /health 等探活路径产生误伤）。
#
# 设计：
# - ZADD/ZREMRANGEBYSCORE/ZCARD 用 pipeline 原子提交，窗口内请求数超限即拒绝；
# - 按 user 维度限流（消息发送类接口），匿名接口按 IP；
# - Redis 不可用时 fail-open（放行），只记 warning —— 限流器故障不应导致全站不可用。

from __future__ import annotations

import hashlib

from src.core.exceptions import BizErrorCode, BizException
from loguru import logger

from src.infra.redis_cache import get_redis_client


async def sliding_window_allow(
    key: str,
    limit: int,
    window_seconds: int,
    *,
    fail_open: bool = True,
) -> bool:
    """滑动窗口限流判定。返回 True=放行，False=超限。"""
    redis = await get_redis_client()
    now_ms = int(_now_ms())
    window_start = now_ms - window_seconds * 1000
    try:
        pipe = redis.pipeline(transaction=True)
        pipe.zadd(key, {f"{now_ms}:{id(object())}": now_ms})
        pipe.zremrangebyscore(key, 0, window_start)
        pipe.zcard(key)
        pipe.expire(key, window_seconds + 60)
        _, _, count, _ = await pipe.execute()
    except Exception as e:
        if fail_open:
            logger.warning(f"限流器 Redis 异常，fail-open 放行: {e}")
            return True
        raise
    return count <= limit


def _now_ms() -> float:
    import time
    return time.time() * 1000


async def enforce_user_message_rate(
    user_id: str,
    *,
    limit: int = 30,
    window_seconds: int = 60,
) -> None:
    """聊天消息发送限流：默认每用户 30 条/分钟。超限抛 429。"""
    if not user_id:
        return
    allowed = await sliding_window_allow(
        key=f"rl:msg:{user_id}",
        limit=limit,
        window_seconds=window_seconds,
    )
    if not allowed:
        raise BizException(
            BizErrorCode.RATE_LIMITED, "发送过于频繁，请稍后再试",
            status_code=429, headers={"Retry-After": str(window_seconds)},
        )


async def enforce_login_rate(
    account: str,
    ip: str,
    *,
    limit: int = 5,
    window_seconds: int = 300,
) -> None:
    """登录 / 注册限流：按「账号」与「IP」双维度，任一超限即拒绝。

    与聊天限流不同，这里 fail-closed：Redis 不可用时拒绝而不是放行 ——
    登录是撞库的唯一入口，宁可短暂不可用也不能放开暴力破解。
    """
    keys = [f"rl:login:ip:{ip or 'unknown'}"]
    if account:
        # 账号做 hash 再入 key：避免手机号/用户名出现在 Redis key 与慢日志里
        digest = hashlib.sha256(account.strip().lower().encode("utf-8")).hexdigest()[:24]
        keys.append(f"rl:login:acct:{digest}")

    for key in keys:
        try:
            allowed = await sliding_window_allow(
                key=key, limit=limit, window_seconds=window_seconds, fail_open=False,
            )
        except Exception as e:
            logger.error(f"登录限流器不可用，按 fail-closed 拒绝: {e}")
            raise BizException(
                BizErrorCode.UPSTREAM_UNAVAILABLE, "登录服务暂时不可用，请稍后重试",
                status_code=503,
            ) from None
        if not allowed:
            raise BizException(
                BizErrorCode.RATE_LIMITED, "尝试过于频繁，请稍后再试",
                status_code=429, headers={"Retry-After": str(window_seconds)},
            )
