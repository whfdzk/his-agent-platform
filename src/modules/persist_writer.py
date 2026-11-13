# src/modules/persist_writer.py
#
# 批量持久化队列的落库实现。
#
# 放在 modules 层：只有这里知道具体的表模型。core 的 persist_queue 只负责
# 排队与攒批，落库实现通过 set_writer() 注入，避免 core 反向依赖 infra/modules。
#
# 落库失败的策略与改造前一致：重试若干次后只记日志并丢弃。
# 这里刻意不把失败抛回队列 —— DB 长时间不可用时重新入队会变成无限重试循环。

from __future__ import annotations

import asyncio

from loguru import logger
from sqlalchemy import case, func, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.core.persist_queue import MAX_RETRIES, PersistItem
from src.infra.database import AsyncSessionLocal
from src.modules.chat.model import ChatMessage, Conversation
from src.modules.ops.model import TokenUsage
from src.modules.ops.trace_model import AgentSpan, AgentTrace, SafetyEvent


def _conversation_rollup(messages: list[dict]) -> dict[int, dict]:
    """按会话汇总本批消息：最大 seq、最后活跃时间、首条用户消息（作标题）。"""
    rollup: dict[int, dict] = {}
    for m in messages:
        conv_id = m.get("conversation_id")
        if conv_id is None:
            continue
        entry = rollup.setdefault(
            conv_id, {"last_seq": 0, "title": "", "first_user_seq": None}
        )
        seq = m.get("seq") or 0
        entry["last_seq"] = max(entry["last_seq"], seq)
        if m.get("role") == "user" and (m.get("content") or "").strip():
            if entry["first_user_seq"] is None or seq < entry["first_user_seq"]:
                entry["first_user_seq"] = seq
                entry["title"] = (m["content"] or "").strip()[:50]
    return rollup


async def _touch_conversations(session, messages: list[dict]) -> None:
    """更新会话的 last_seq / last_active_at / 首条消息标题。

    会话列表依赖这三个字段：不维护的话会话时间恒为空、标题恒为空，
    前端只能靠本地 localStorage 兜底，换设备就看不到历史。
    """
    for conv_id, info in _conversation_rollup(messages).items():
        values = {
            "last_seq": func.greatest(Conversation.last_seq, info["last_seq"]),
            "last_active_at": func.now(),
        }
        if info["title"]:
            # 标题只填空值，不覆盖已有人工/AI 标题
            values["title"] = case(
                (Conversation.title == "", info["title"]),
                else_=Conversation.title,
            )
        await session.execute(
            update(Conversation).where(Conversation.id == conv_id).values(**values)
        )


async def _insert_messages(messages: list[dict]) -> None:
    """chat_messages：按 (conversation_id, seq) 幂等去重批量插入。"""
    for attempt in range(MAX_RETRIES):
        try:
            async with AsyncSessionLocal() as session:
                stmt = pg_insert(ChatMessage).values(messages)
                stmt = stmt.on_conflict_do_nothing(
                    index_elements=["conversation_id", "seq"]
                )
                await session.execute(stmt)
                await _touch_conversations(session, messages)
                await session.commit()
            return
        except Exception as e:
            if attempt == MAX_RETRIES - 1:
                logger.warning(f"chat_messages 批量落库失败（丢弃 {len(messages)} 条）: {e}")
                return
            await asyncio.sleep(0.2 * (attempt + 1))


async def _insert_usages(usages: list[dict]) -> None:
    """token_usage：普通批量 insert。"""
    for attempt in range(MAX_RETRIES):
        try:
            async with AsyncSessionLocal() as session:
                session.add_all([TokenUsage(**u) for u in usages])
                await session.commit()
            return
        except Exception as e:
            if attempt == MAX_RETRIES - 1:
                logger.warning(f"token_usage 批量落库失败（丢弃 {len(usages)} 条）: {e}")
                return
            await asyncio.sleep(0.2 * (attempt + 1))


async def _insert_traces(traces: list[dict]) -> None:
    """agent_traces：按 trace_id 幂等 upsert（同轮重复 finish 只更新聚合字段）。"""
    for attempt in range(MAX_RETRIES):
        try:
            async with AsyncSessionLocal() as session:
                stmt = pg_insert(AgentTrace).values(traces)
                update_cols = {
                    c: getattr(stmt.excluded, c)
                    for c in (
                        "status", "error_type", "latency_ms", "prompt_tokens",
                        "completion_tokens", "total_tokens", "tool_calls", "tool_names",
                        "agent_calls", "retrieval_count", "retrieval_sources",
                        "safety_level", "safety_result", "safety_rules",
                        "input_chars", "output_chars", "model_name", "model_version",
                    )
                }
                await session.execute(
                    stmt.on_conflict_do_update(index_elements=["trace_id"], set_=update_cols)
                )
                await session.commit()
            return
        except Exception as e:
            if attempt == MAX_RETRIES - 1:
                logger.warning(f"agent_traces 落库失败（丢弃 {len(traces)} 条）: {e}")
                return
            await asyncio.sleep(0.2 * (attempt + 1))


async def _insert_spans(spans: list[dict]) -> None:
    for attempt in range(MAX_RETRIES):
        try:
            async with AsyncSessionLocal() as session:
                session.add_all([AgentSpan(**s) for s in spans])
                await session.commit()
            return
        except Exception as e:
            if attempt == MAX_RETRIES - 1:
                logger.warning(f"agent_spans 落库失败（丢弃 {len(spans)} 条）: {e}")
                return
            await asyncio.sleep(0.2 * (attempt + 1))


async def _insert_safety_events(events: list[dict]) -> None:
    for attempt in range(MAX_RETRIES):
        try:
            async with AsyncSessionLocal() as session:
                session.add_all([SafetyEvent(**ev) for ev in events])
                await session.commit()
            return
        except Exception as e:
            if attempt == MAX_RETRIES - 1:
                logger.warning(f"safety_events 落库失败（丢弃 {len(events)} 条）: {e}")
                return
            await asyncio.sleep(0.2 * (attempt + 1))


async def write_batch(batch: list[PersistItem]) -> None:
    """消费一个批次：按类型分组后各自批量写入。"""
    messages = [i.payload for i in batch if i.kind == "chat_message"]
    usages = [i.payload for i in batch if i.kind == "token_usage"]
    traces = [i.payload for i in batch if i.kind == "agent_trace"]
    spans = [i.payload for i in batch if i.kind == "agent_span"]
    safety_events = [i.payload for i in batch if i.kind == "safety_event"]
    if messages:
        await _insert_messages(messages)
    if usages:
        await _insert_usages(usages)
    if traces:
        await _insert_traces(traces)
    if spans:
        await _insert_spans(spans)
    if safety_events:
        await _insert_safety_events(safety_events)
