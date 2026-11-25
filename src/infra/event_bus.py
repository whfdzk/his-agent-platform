# src/infra/event_bus.py
#
# 跨进程事件总线（Redis Stream）。
#
# 为什么是 Redis Stream 而不是 Kafka：
#   本项目已是单机 Docker 部署，Redis 本来就在跑（限流、SSE、checkpoint 都在用）；
#   Kafka 要再引入 ZK/KRaft 一套，运维成本与"多实例解耦"的收益不成比例。
#   Redis Stream 具备消费组、ACK、Pending 列表与 XAUTOCLAIM，语义上覆盖了
#   本项目需要的那部分（至少一次 + 手动 ACK + 失败重投 + 死信），
#   面试时讲清"为什么不用 Kafka"比硬塞一个 Kafka 更值。
#
# 语义约定：
#   - 至少一次（at-least-once）：消费成功才 XACK；处理失败留在 Pending，可由
#     任意实例 XAUTOCLAIM 接管重投；
#   - 生产端幂等：publish 带 dedupe_key 时用 SETNX 去重，进程重启/重试不会重复投递；
#   - 消费端幂等：由消费方按 (event_id, consumer) 落唯一约束，重复投递只执行一次；
#   - 死信：投递次数超过上限后转 his:events:dead 并 ACK，避免毒消息卡住消费组。

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from loguru import logger

STREAM_KEY = "his:events"
DEAD_KEY = "his:events:dead"
# 单流上限，防止 Redis 内存被无限吃掉（近似裁剪按整节点，容忍误差）
STREAM_MAXLEN = 100_000
# 生产端去重窗口：24h 内同一 dedupe_key 只投一次
PUBLISH_DEDUPE_TTL = 86_400


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def _redis():
    from src.infra.redis_cache import get_redis_client

    return await get_redis_client()


async def publish(
    event_type: str,
    payload: dict[str, Any] | None = None,
    *,
    dedupe_key: str = "",
    tenant_id: int = 1,
) -> str | None:
    """投递一条事件；Redis 不可用时返回 None，不打断业务主流程。

    返回值是事件 ID（`{type}:{dedupe_key|uuid}`），可用于日志关联与幂等核对。
    """
    if not event_type:
        raise ValueError("event_type 不能为空")
    event_id = f"{event_type}:{dedupe_key}" if dedupe_key else f"{event_type}:{_uuid()}"

    try:
        r = await _redis()
        if dedupe_key:
            # SETNX 成功才继续投递：重复触发的业务（LangGraph 重跑节点、
            # 客户端重试）不会在流里出现两条一样的事件
            got = await r.set(f"ev:pub:{event_id}", "1", nx=True, ex=PUBLISH_DEDUPE_TTL)
            if not got:
                logger.info(f"事件已投递过，跳过 {event_id}")
                return None
        await r.xadd(
            STREAM_KEY,
            {
                "id": event_id,
                "type": event_type,
                "tenant_id": str(int(tenant_id)),
                "ts": _now(),
                "payload": json.dumps(payload or {}, ensure_ascii=False, default=str),
            },
            maxlen=STREAM_MAXLEN,
            approximate=True,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"事件投递失败（{event_type}）：{type(e).__name__}: {e}")
        return None
    logger.info(f"事件已投递 {event_id}")
    return event_id


def _uuid() -> str:
    from uuid import uuid4

    return uuid4().hex


async def ensure_group(group: str, stream: str = STREAM_KEY) -> None:
    r = await _redis()
    try:
        await r.xgroup_create(stream, group, id="0", mkstream=True)
    except Exception as e:  # BUSYGROUP = 组已存在
        if "BUSYGROUP" not in str(e):
            raise


async def read_group(
    group: str, consumer: str, *, count: int = 10, block_ms: int = 2000,
    stream: str = STREAM_KEY,
) -> list[tuple[str, dict]]:
    """读新消息（'>' 表示从未投递给该组、或上次未 ACK 之后的未投递部分）。"""
    r = await _redis()
    resp = await r.xreadgroup(group, consumer, {stream: ">"}, count=count, block=block_ms)
    return _flatten(resp)


async def claim_stale(
    group: str, consumer: str, *, min_idle_ms: int = 30_000, count: int = 10,
    stream: str = STREAM_KEY,
) -> list[tuple[str, dict]]:
    """接管「已投递但超过 min_idle 还没 ACK」的消息（消费进程崩了/超时）。"""
    r = await _redis()
    try:
        # redis-py 返回 (next_cursor, messages, deleted_ids)
        resp = await r.xautoclaim(stream, group, consumer, min_idle_time=min_idle_ms,
                                  start_id="0-0", count=count)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"XAUTOCLAIM 失败: {type(e).__name__}: {e}")
        return []
    flat = _flatten(resp)
    return flat


def _flatten(resp: Any) -> list[tuple[str, dict]]:
    out: list[tuple[str, dict]] = []
    if not resp:
        return out
    # xreadgroup: [(stream, [(id, fields), ...])]
    if isinstance(resp, list) and resp and isinstance(resp[0], (list, tuple)) and len(resp[0]) == 2 \
            and isinstance(resp[0][1], list):
        for _stream, entries in resp:
            for mid, fields in entries:
                out.append((mid, fields))
        return out
    # xautoclaim: (next_cursor, [(id, fields), ...], deleted)
    for item in resp:
        if isinstance(item, list):
            for mid, fields in item:
                out.append((mid, fields))
    return out


async def ack(group: str, msg_id: str, stream: str = STREAM_KEY) -> None:
    r = await _redis()
    await r.xack(stream, group, msg_id)


async def deliveries(group: str, msg_id: str, stream: str = STREAM_KEY) -> int:
    """该消息被投递过几次（XPENDING 的 times_delivered）。查不到按 1 处理。"""
    r = await _redis()
    try:
        rows = await r.xpending_range(stream, group, min=msg_id, max=msg_id, count=1)
        if rows:
            return int(rows[0].get("times_delivered") or 1)
    except Exception:  # noqa: BLE001
        pass
    return 1


async def to_dead_letter(group: str, msg_id: str, fields: dict, reason: str) -> None:
    r = await _redis()
    try:
        await r.xadd(
            DEAD_KEY,
            {
                **{k: str(v) for k, v in fields.items()},
                "failed_group": group,
                "failed_msg_id": msg_id,
                "reason": reason[:500],
                "failed_at": _now(),
            },
            maxlen=10_000,
            approximate=True,
        )
        await ack(group, msg_id)
    except Exception as e:  # noqa: BLE001
        logger.error(f"转死信失败 {msg_id}: {type(e).__name__}: {e}")


async def stats(group: str, stream: str = STREAM_KEY) -> dict:
    """管理端观测用：流长度、消费组进度、pending 数与死信数。"""
    r = await _redis()
    out: dict[str, Any] = {"stream": stream, "length": 0, "group": group,
                           "pending": 0, "lag": 0, "dead": 0}
    try:
        out["length"] = int(await r.xlen(stream))
        out["dead"] = int(await r.xlen(DEAD_KEY))
        groups = await r.xinfo_groups(stream)
        for g in groups:
            if g.get("name") == group:
                out["pending"] = int(g.get("pending") or 0)
                out["lag"] = int(g.get("lag") or 0)
                out["consumers"] = int(g.get("consumers") or 0)
                out["last_delivered_id"] = g.get("last-delivered-id") or ""
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {e}"
    return out
