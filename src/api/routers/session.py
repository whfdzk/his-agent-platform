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
from src.core.exceptions import BizErrorCode, BizException
from src.infra.database import get_db, AsyncSessionLocal
from src.infra.redis_cache import get_checkpointer_redis
from src.modules.medical.model import Consultation
from src.api.deps import AuthContext, get_current_user_id, require_auth, require_scope

router = APIRouter(
    prefix="/api/v1/sessions", tags=["sessions"],
    # 会话历史属于问诊数据：B1 起按 chat:read 收紧（护士只读、患者本人可读）
    dependencies=[Depends(require_scope("chat:read"))],
)


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
async def list_sessions(limit: int = 50, ctx: AuthContext = Depends(require_auth())):
    """
    会话列表（仅当前登录用户自己的会话，user_id 从 JWT 取，不可伪造）。
    - 活跃问诊会话：SCAN Redis inquiry_state:{user_id}:{session_id} + MGET 批量获取
    - 历史会话：consultations 表 + conversations 表（同一 DB 连接查询）
    - 加 Redis 缓存（TTL 30s），减少重复查询

    C1：会话查询除 user_id 还带 tenant_id —— 单靠 user_id 时，
    同一主体 id 在别的租户里存在同号记录（影子数据）会串号。
    """
    user_id = ctx.subject_id
    tenant_id = ctx.tenant_id
    redis = get_checkpointer_redis()

    # 缓存键带租户：不同租户下同 user_id 的列表不能互相命中
    cache_key = f"sessions_list:{tenant_id}:{user_id}:{limit}"
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
            q = (
                select(Consultation)
                .where(Consultation.tenant_id == tenant_id)
                .order_by(Consultation.created_at.desc())
                .limit(limit * 2)
            )
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
            # 优化：去掉标题兜底查询（懒加载，不在列表接口里查）
            from src.modules.chat.model import Conversation
            convs = (await db.execute(
                select(Conversation)
                .where(Conversation.user_id == user_id, Conversation.tenant_id == tenant_id)
                .order_by(Conversation.last_active_at.desc().nullslast(),
                          Conversation.updated_at.desc())
                .limit(limit)
            )).scalars().all()

            for conv in convs:
                sid = conv.session_id
                if not sid or sid in sessions:
                    continue
                sessions[sid] = {
                    "session_id": sid,
                    "user_id": user_id,
                    "title": conv.title or "历史会话",
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

    # 写入缓存（TTL 5分钟，减少重复查询）
    import json
    await redis.setex(cache_key, 300, json.dumps(result, ensure_ascii=False))

    return result


@router.get("/{session_id}/messages")
async def get_session_messages(session_id: str, ctx: AuthContext = Depends(require_auth())):
    """
    会话消息历史（只读回放，仅本人会话）。
    数据源优先级：PG chat_messages（source of truth）→ checkpointer / InquiryState 补充；
    两路数据按 (role, content) 去重。

    C1：归属判定是 (tenant_id, session_id) + 本人 user_id。
    只比对 user_id 时，另一租户里同 user_id 的会话也会被读出来（200 而非 404）。
    """
    user_id = ctx.subject_id
    tenant_id = ctx.tenant_id
    thread_id = f"{user_id}:{session_id}"
    messages: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def _push(d: dict | None) -> None:
        if not d:
            return
        content = d["content"].strip()
        if not content:
            return
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
        messages.append(
            {
                "role": d["role"],
                "content": d["content"],
                # seq 与 event_type 只有 PG 有；checkpointer / InquiryState 补出来的消息
                # 没有这些字段，前端按 event_type 缺省成普通气泡。
                "seq": d.get("seq"),
                "event_type": d.get("event_type"),
            }
        )

    # 1. PG 持久化消息（source of truth）
    try:
        from src.modules.chat.model import Conversation, ChatMessage

        async with AsyncSessionLocal() as db:
            # 先按 session_id 查会话（不带租户条件），归属不对直接 404：
            # - 别的租户里有这个 session_id → 404（不能因为"不在我的租户"就当会话不存在返回空列表，
            #   那样调用方会以为读到了别人的会话且内容为空，语义错误）
            # - 同租户但不是本人 → 404（不暴露存在性）
            # 查不到任何会话才继续走 checkpointer / InquiryState（thread_id 已按 user_id 隔离）
            conv = (await db.execute(
                select(Conversation).where(Conversation.session_id == session_id)
            )).scalars().first()
            if conv is not None and (
                int(conv.tenant_id) != int(tenant_id) or str(conv.user_id) != str(user_id)
            ):
                raise BizException(BizErrorCode.NOT_FOUND, "会话不存在", status_code=404)
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
                    # content 用脱敏列：回放属于对外展示，原文列只留审计追溯
                    _push(
                        {
                            "role": m.role,
                            "content": m.content_redacted or m.content,
                            "seq": int(m.seq),
                            "event_type": str(m.event_type or "token"),
                        }
                    )
    except BizException:
        raise
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
