# src/api/routers/sse.py
#
# SSE 帧构造与单轮 seq 分配。
#
# 单独成模块的意义：这些都是无外部依赖的纯逻辑。抽出来之后可以脱离 chat 路由
# 直接单测 —— 而 chat 路由一导入就会拉起整条 agent / LLM 依赖链。

from __future__ import annotations

import json

from loguru import logger

# seq 计数器 TTL：与会话缓存同量级。此前不回写 TTL，key 永久驻留；
# 一旦 Redis 重启/淘汰，计数从 0 重来会与 DB 唯一键冲突并丢消息。
SEQ_KEY_TTL_SECONDS = 7 * 86400


def sse_frame(data: dict, seq: int | None = None) -> str:
    """构造 SSE 帧；seq 存在时输出 id 行（断线续传用，旧前端自动忽略）。"""
    payload = f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
    if seq is not None:
        return f"id: {seq}\n{payload}"
    return payload


def error_event(code: str, message: str, retryable: bool = True) -> dict:
    return {"type": "error", "code": code, "message": message, "retryable": retryable}


class SseWriter:
    """单轮对话的 SSE 写入器：负责 seq 分配与帧序列化。

    seq 采用"本地自增 + 整轮结束时回写"。同会话互斥锁（lock:turn）保证同一
    时刻只有一个轮次在推进该计数，本地自增因此不会与其他轮次交叉。
    逐 token 走 Redis INCR 时，200 token 的回复就是 200 次串行往返。
    """

    def __init__(self, redis, seq_key: str, start: int = 0) -> None:
        self._redis = redis
        self._seq_key = seq_key
        self._next = start + 1

    def take(self) -> int:
        """取下一个 seq（不生成帧），供落库等场景复用同一个计数。"""
        seq = self._next
        self._next += 1
        return seq

    def frame(self, event: dict, with_id: bool = True) -> str:
        """序列化事件；with_id=False 用于不需要续传序号的帧。"""
        if not with_id:
            return sse_frame(event)
        return sse_frame(event, seq=self.take())

    async def sync(self) -> None:
        """把本轮已分配到的最大 seq 回写 Redis；失败不影响主链路。"""
        try:
            await self._redis.set(
                self._seq_key, self._next - 1, ex=SEQ_KEY_TTL_SECONDS,
            )
        except Exception as e:
            logger.warning(f"seq 计数回写失败: {e}")
