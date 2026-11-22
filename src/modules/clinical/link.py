# src/modules/clinical/link.py
#
# 把"当前会话有哪个动作待确认"记到 Redis，供 chat 层在收尾时下发确认卡片。
#
# 为什么用 Redis 而不是查库：
#   chat 层的 SSE 收尾在流式循环里，已经持有连接、不能再插一次 DB 查询；
#   而工具层（guide_tools / worker_tools）创建草稿时正好在 Redis 上下文里。
#   所以这里只做"指针"：真正的状态与审计在 clinical_actions 表，Redis 里只存 action_id。
#
# 一致性：pending 指针过期或被清掉不影响动作本身 —— 动作仍在库里等确认，
#        患者也可以通过 /clinical-actions/pending 重新拿到。

from __future__ import annotations

from loguru import logger

PENDING_TTL_SECONDS = 900


def _key(thread_id: str) -> str:
    return f"pending_action:{thread_id}"


async def remember_pending(thread_id: str, action_id: int, ttl: int = PENDING_TTL_SECONDS) -> None:
    if not thread_id:
        return
    try:
        from src.infra.redis_cache import get_checkpointer_redis

        redis = get_checkpointer_redis()
        await redis.set(_key(thread_id), str(action_id), ex=max(60, ttl))
    except Exception as e:  # noqa: BLE001 —— 只影响"能不能主动推确认卡片"，不影响动作落地
        logger.warning(f"记录待确认指针失败 thread={thread_id}: {type(e).__name__}: {e}")


async def read_pending(thread_id: str) -> int | None:
    if not thread_id:
        return None
    try:
        from src.infra.redis_cache import get_checkpointer_redis

        redis = get_checkpointer_redis()
        raw = await redis.get(_key(thread_id))
        if not raw:
            return None
        return int(raw.decode() if isinstance(raw, bytes) else raw)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"读取待确认指针失败 thread={thread_id}: {type(e).__name__}: {e}")
        return None


async def forget_pending(thread_id: str) -> None:
    if not thread_id:
        return
    try:
        from src.infra.redis_cache import get_checkpointer_redis

        redis = get_checkpointer_redis()
        await redis.delete(_key(thread_id))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"清除待确认指针失败 thread={thread_id}: {type(e).__name__}: {e}")
