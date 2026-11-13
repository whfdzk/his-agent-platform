# src/api/routers/session.py
#
# 会话历史 API：
# - GET /api/v1/sessions                      ：会话列表（Redis 活跃问诊会话 + consultations 落库记录合并）
# - GET /api/v1/sessions/{session_id}/messages：会话消息历史（LangGraph Redis checkpointer）
#
# 局限说明：AsyncRedisSaver 不支持列举 thread，且问诊活跃标记 TTL 1 小时，
# 因此会话列表可能不完整（历史会话仅能从 consultations 补全）。

from __future__ import annotations

from datetime import timezone

from fastapi import APIRouter, Depends
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.agents.supervisor_agent import get_supervisor_checkpointer
from src.agents.inquiry.state import InquiryState
from src.infra.database import get_db, AsyncSessionLocal
from src.infra.redis_cache import get_checkpointer_redis
from src.modules.medical.model import Consultation
from src.api.deps import get_current_user_id

router = APIRouter(prefix="/api/v1/sessions", tags=["sessions"])


def _msg_to_dict(m) -> dict | None:
    """把 LangChain Message 序列化为 {role, content}，只保留 human/ai。"""
    mtype = getattr(m, "type", "")
    if mtype not in ("human", "ai"):
        return None
    content = m.content
    if not isinstance(content, str):
        # 多模态 content 为 list[{type:text, text:...}]
        content = "".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        )
    if not content:
        return None
    return {"role": "user" if mtype == "human" else "assistant", "content": content}


def _utc_iso(dt) -> str | None:
    """DB 里是 naive UTC 时间戳：补上时区再下发，前端才能按本地时区渲染。

    直接 str(dt) 会让浏览器把 UTC 当本地时间显示（东八区差 8 小时）。
    """
    if dt is None:
        return None
    aware = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    return aware.isoformat()


async def _inquiry_state_title(raw) -> str:
    """从 Redis 中的问诊状态提取首条用户消息作为标题。"""
    try:
        state = InquiryState.model_validate_json(raw)
        for m in state.messages:
            d = _msg_to_dict(m)
            if d and d["role"] == "user":
                return d["content"][:50]
    except Exception:
        pass
    return "问诊进行中"


@router.get("")
async def list_sessions(limit: int = 50, current_user: str = Depends(get_current_user_id)):
    """
    会话列表（仅当前登录用户自己的会话，user_id 从 JWT 取，不可伪造）。
    - 活跃问诊会话：SCAN Redis inquiry_state:{user_id}:{session_id} + MGET 批量获取
    - 历史会话：consultations 表 + conversations 表（同一 DB 连接查询）
    - 加 Redis 缓存（TTL 30s），减少重复查询
    """
    user_id = current_user
    redis = get_checkpointer_redis()

    # 缓存键：sessions_list:{user_id}:{limit}
    cache_key = f"sessions_list:{user_id}:{limit}"
    cached = await redis.get(cache_key)
    if cached:
        import json
        return json.loads(cached)

    sessions: dict[str, dict] = {}

    # 1. 活跃问诊会话：SCAN + MGET 批量获取（优化 N+1）
    pattern = f"inquiry_state:{user_id}:*" if user_id else "inquiry_state:*"
    keys = []
    async for key in redis.scan_iter(match=pattern, count=100):
        full_key = key.decode() if isinstance(key, bytes) else key
        keys.append(full_key)

    if keys:
        # 批量获取所有 key 的值
        raw_values = await redis.mget(keys)
        for i, full_key in enumerate(keys):
            raw = raw_values[i]
            # key 形如 inquiry_state:{user_id}:{session_id}
            parts = full_key.split(":")
            sid = parts[-1] if len(parts) >= 3 else full_key
            uid = parts[1] if len(parts) >= 3 else ""
            sessions[sid] = {
                "session_id": sid,
                "user_id": uid,
                "title": await _inquiry_state_title(raw) if raw else "问诊进行中",
                "is_inquiry_active": True,
                "last_active_at": None,
            }

    # 2. 落库的历史问诊 + 普通聊天会话（合并到一次 DB 连接）
    try:
        async with AsyncSessionLocal() as db:
            # 2.1 consultations 表
            q = select(Consultation).order_by(Consultation.created_at.desc()).limit(limit * 2)
            if user_id.isdigit():
                q = q.where(Consultation.patient_id == int(user_id))
            rows = (await db.execute(q)).scalars().all()
            for c in rows:
                if not c.session_id or c.session_id in sessions:
                    continue
                sessions[c.session_id] = {
                    "session_id": c.session_id,
                    "user_id": user_id,
                    "title": (c.chief_complaint or c.diagnosis or "历史问诊")[:50],
                    "is_inquiry_active": False,
                    "last_active_at": _utc_iso(c.created_at),
                }

            # 2.2 conversations 表（同一连接）
            from src.modules.chat.model import ChatMessage, Conversation
            convs = (await db.execute(
                select(Conversation)
                .where(Conversation.user_id == user_id)
                .order_by(Conversation.last_active_at.desc().nullslast(),
                          Conversation.updated_at.desc())
                .limit(limit)
            )).scalars().all()

            # 标题兜底：conversations.title 为空时取首条用户消息
            missing = [c.id for c in convs if not c.title]
            title_map: dict[int, str] = {}
            if missing:
                first_msgs = (await db.execute(
                    select(ChatMessage.conversation_id, ChatMessage.content)
                    .where(
                        ChatMessage.conversation_id.in_(missing),
                        ChatMessage.role == "user",
                        ChatMessage.content != "",
                    )
                    .order_by(ChatMessage.conversation_id, ChatMessage.seq.asc())
                )).all()
                for conv_id, content in first_msgs:
                    title_map.setdefault(conv_id, (content or "").strip()[:50])

            for conv in convs:
                sid = conv.session_id
                if not sid or sid in sessions:
                    continue
                sessions[sid] = {
                    "session_id": sid,
                    "user_id": user_id,
                    "title": conv.title or title_map.get(conv.id) or "历史会话",
                    "is_inquiry_active": False,
                    "last_active_at": _utc_iso(conv.last_active_at or conv.updated_at),
                }
    except Exception as e:
        logger.warning(f"会话列表 DB 查询失败: {e}")

    items = sorted(
        sessions.values(),
        key=lambda s: s["last_active_at"] or "",
        reverse=True,
    )
    result = {"sessions": items[:limit], "total": len(items)}

    # 写入缓存（TTL 30s）
    import json
    await redis.setex(cache_key, 30, json.dumps(result, ensure_ascii=False))

    return result


@router.get("/{session_id}/messages")
async def get_session_messages(session_id: str, current_user: str = Depends(get_current_user_id)):
    """
    会话消息历史（只读回放，仅本人会话）。
    数据源优先级：PG chat_messages（source of truth）→ checkpointer / InquiryState 补充；
    两路数据按 (role, content) 去重。
    """
    user_id = current_user
    thread_id = f"{user_id}:{session_id}"
    messages: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def _push(d: dict | None) -> None:
        if not d:
            return
        content = d["content"].strip()
        key = (d["role"], content)
        if key in seen:
            return
        # 跨源去重：checkpointer 存的是模型原始输出，PG 存的是输出审核后的文本
        # （可能追加免责声明），全文不等但属于同一条消息，按前缀判定避免重复展示。
        # 只对足够长的文本做前缀判定，"你好" 这类短消息仍按全文精确去重。
        if len(content) >= 20:
            for role, prev in seen:
                if role != d["role"] or len(prev) < 20:
                    continue
                if prev.startswith(content) or content.startswith(prev):
                    return
        seen.add(key)
        messages.append(d)

    # 1. PG 持久化消息（source of truth）
    try:
        from src.modules.chat.model import Conversation, ChatMessage

        async with AsyncSessionLocal() as db:
            conv = (await db.execute(
                select(Conversation).where(
                    Conversation.user_id == user_id,
                    Conversation.session_id == session_id,
                )
            )).scalar_one_or_none()
            if conv is not None:
                rows = (await db.execute(
                    select(ChatMessage)
                    .where(
                        ChatMessage.conversation_id == conv.id,
                        ChatMessage.role.in_(["user", "assistant"]),
                        ChatMessage.content != "",
                    )
                    .order_by(ChatMessage.seq.asc())
                    .limit(500)
                )).scalars().all()
                for m in rows:
                    _push({"role": m.role, "content": m.content})
    except Exception as e:
        logger.warning(f"chat_messages 历史读取失败: {e}")

    # 2. Supervisor checkpointer 中的消息
    try:
        checkpointer = await get_supervisor_checkpointer()
        tup = await checkpointer.aget_tuple({"configurable": {"thread_id": thread_id}})
        if tup and tup.checkpoint:
            channel_values = tup.checkpoint.get("channel_values") or {}
            raw_msgs = channel_values.get("messages") or []
            for m in raw_msgs:
                _push(_msg_to_dict(m))
    except Exception as e:
        logger.warning(f"checkpointer 消息读取失败: {e}")

    # 3. 问诊活跃会话：叠加 InquiryState 的消息
    redis = get_checkpointer_redis()
    is_active = bool(await redis.exists(f"inquiry_active:{thread_id}"))
    if is_active:
        raw = await redis.get(f"inquiry_state:{thread_id}")
        if raw:
            try:
                state = InquiryState.model_validate_json(raw)
                for m in state.messages:
                    _push(_msg_to_dict(m))
            except Exception as e:
                logger.warning(f"inquiry_state 消息解析失败: {e}")

    return {"messages": messages, "is_inquiry_active": is_active, "session_id": session_id}
