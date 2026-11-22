"""会话内消息序号（seq）的统一分配器。

为什么单独抽出来：AI 侧的 SSE 每帧要占一个 seq（前端用它做断线重放），
坐席侧的回复也要写进同一个 `chat_messages`，两处必须共用同一个计数器，
否则会各自从 `conversation.last_seq` 起算，出现同 seq 两条消息，
被 `on_conflict_do_nothing` 静默丢掉一条（患者会看到"少了一句话"）。

计数器的规则：
- Redis 里 `chat:seq:{user_id}:{session_id}` 是唯一真源（原子 INCR）。
- key 丢失（重启/淘汰）时用 DB 里该会话已落库的最大 seq 回填，避免从 0 重来。
"""

import logging

logger = logging.getLogger(__name__)

SEQ_TTL_SECONDS = 7 * 86400


def _decode(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "ignore")
    return str(value)


def _key(user_id: str, session_id: str) -> str:
    return f"chat:seq:{user_id}:{session_id}"


async def ensure_seq_base(redis, user_id: str, session_id: str, *, load_base=None) -> int:
    """返回会话当前已分配到的最大 seq。

    回填数据源由调用方注入（`load_base`）：core 层不能反向依赖 infra / modules，
    所以这里只负责"读缓存 → 缓存没有再问注入的 loader → 写回"，具体怎么查库
    由 modules 层的 `seq_allocator` 提供。
    """
    key = _key(user_id, session_id)
    try:
        raw = _decode(await redis.get(key))
        if raw:
            return int(raw)
    except Exception as e:
        logger.warning(f"seq 缓存读取失败，尝试用 DB 回填: {e}")

    base = 0
    if load_base is not None:
        try:
            base = int(await load_base(user_id, session_id))
        except Exception as e:
            logger.warning(f"seq 回填失败，按 0 起算: {e}")

    try:
        await redis.set(key, str(base), ex=SEQ_TTL_SECONDS)
    except Exception as e:
        logger.warning(f"seq 基线写回失败: {e}")
    return base


async def next_seq(redis, user_id: str, session_id: str, *, load_base=None) -> int:
    """原子分配下一个 seq，并保证计数器带 TTL，不会永久驻留。"""
    await ensure_seq_base(redis, user_id, session_id, load_base=load_base)
    seq = int(await redis.incr(_key(user_id, session_id)))
    try:
        await redis.expire(_key(user_id, session_id), SEQ_TTL_SECONDS)
    except Exception:
        pass
    return seq
