# src/api/routers/chat.py
#
# 聊天路由（生产化改造版）：
# - 可选鉴权：登录用户 user_id 从 Bearer Token 解出（禁止信任请求体自报字段）；
#   未登录用户以 "anon" 身份聊天 —— 可以咨询，但不落库、不能新建会话，
#   只有 Redis 中的会话级短期记忆（1 小时）
# - SSE 流：首字节 ping + 15s 心跳 + 断连检测 + 单轮 TURN_TIMEOUT 总超时，断连即取消 LLM 任务
# - 幂等：client_message_id + Redis SETNX（5 分钟窗口），重复请求直接重放上次回复
# - 限流：登录按用户、游客按 IP 滑动窗口（30 msg/min）+ 进程内 SSE 连接数上限
# - 并发：per-session Redis 互斥锁，防止同会话并发消息交叉覆盖状态
# - 持久化：登录用户消息经批量队列落库 PG（source of truth），SSE 事件带递增 seq 支持断线续传

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from langchain_core.messages import AIMessage
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from loguru import logger

from src.infra.database import AsyncSessionLocal
from src.infra.redis_cache import get_checkpointer_redis
from src.agents.supervisor_agent import get_supervisor_agent
from src.core.context import UserContext
from src.agents.inquiry.graph import run_inquiry
from src.agents.inquiry.deps import build_inquiry_deps
from src.agents.inquiry.state import InquiryState, InquiryPhase
from src.agents.workers.inquiry_agent import handle_handoff
from src.core.llm_callbacks import make_usage_handler
from src.core.concurrency import guarded, guarded_stream, sse_limiter, SystemBusyError, request_id_var
from src.core.exceptions import BizErrorCode, BizException
from src.core.limits import (
    BudgetExceeded,
    acquire_user_slot,
    current_budget,
    release_user_slot,
    reset_budget,
    start_budget,
)
from src.core.safety import record_safety_event, safety_engine
from src.core.llm_guard import scrub_for_llm
from src.core.tracing import end_trace, start_trace, trace_span
from src.api.rate_limit import enforce_user_message_rate
from src.api.deps import get_current_user_id, get_optional_user_id, ANON_USER_ID
from src.api.routers.sse import SseWriter, error_event, sse_frame
from src.core.config import get_settings

settings = get_settings()
router = APIRouter(prefix="/api/v1/chat", tags=["chat"])

HEARTBEAT_INTERVAL = 15.0          # SSE 心跳间隔（秒），须小于 Nginx 读超时
# 同会话互斥锁 TTL：必须覆盖整轮耗时。此前固定 30s，长轮次进行中锁会先过期，
# 用户补发的消息得以并发跑在同一个 thread_id 上，互相覆盖 checkpoint
_LOCK_TTL_MS = int(settings.TURN_TIMEOUT * 1000) + 30_000
_IDEM_TTL = 300                    # 幂等键保留窗口（秒）


class ChatRequest(BaseModel):
    session_id: str
    message: str
    patient_id: int | None = None
    client_message_id: str | None = Field(
        default=None, max_length=64,
        description="客户端幂等键：同一消息重试时复用，服务端据此去重",
    )


class ChatResponse(BaseModel):
    reply: str
    session_id: str
    authenticated: bool = True


@dataclass
class ChatIdentity:
    """一次聊天请求的身份：登录用户 or 游客。"""
    user_id: str                  # 登录=患者 ID；游客="anon"
    authenticated: bool
    rate_key: str                 # 限流维度：user:<id> / ip:<ip>
    patient_id: int | None        # 患者档案 ID（游客为 None）

    @property
    def persist(self) -> bool:
        """是否允许写持久化数据（会话、消息、问诊记录、预约）。"""
        return self.authenticated


def _resolve_identity(current_user: str | None, request: Request) -> ChatIdentity:
    """解析请求身份：带合法 Token 为登录用户，否则按游客（IP 维度限流）。

    Token 非法/过期由 get_optional_user_id 抛 401，不会静默降级为游客，
    避免"登录态失效"被误当成游客而丢失历史。
    """
    if current_user:
        try:
            patient_id: int | None = int(current_user)
        except (TypeError, ValueError):
            patient_id = None
        return ChatIdentity(
            user_id=str(current_user),
            authenticated=True,
            rate_key=f"user:{current_user}",
            patient_id=patient_id,
        )
    ip = request.client.host if request.client else "unknown"
    return ChatIdentity(
        user_id=ANON_USER_ID,
        authenticated=False,
        rate_key=f"ip:{ip}",
        patient_id=None,
    )


def _thread_id(identity: ChatIdentity, session_id: str) -> str:
    """thread_id：登录用户 "患者ID:会话ID"，游客 "anon:会话ID"。

    游客前缀固定为 anon（患者 ID 为纯数字，永不冲突），
    会话彼此仍由 session_id 隔离，短期记忆互不串扰。
    """
    return f"{identity.user_id}:{session_id}"


# ── SSE 工具 ──────────────────────────────────────────────────────────────

def _friendly_error(e: Exception) -> dict:
    """异常 → 用户友好错误事件；堆栈只进日志，绝不下发客户端。"""
    logger.exception("chat 轮次执行异常")
    if isinstance(e, SystemBusyError):
        return error_event("busy", "当前咨询人数较多，请稍后重试")
    if isinstance(e, asyncio.TimeoutError):
        return error_event("timeout", "本次回复生成超时，请重试")
    return error_event("internal", "服务暂时不可用，请稍后重试")


def _decode(value) -> str:
    """Redis 响应统一解码。

    本路由用的是 checkpointer 连接池（bytes 模式，供 RedisSaver 使用），
    bytes 与 str 比较恒不相等，会让"处理中"的幂等判断失效。
    """
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", errors="ignore")
    return value or ""


# ── 幂等与持久化辅助 ──────────────────────────────────────────────────────

async def _idem_check_and_claim(redis, user_id: str, cmid: str | None) -> str:
    """幂等认领。返回 "claimed" | "processing" | 存量回复 JSON。"""
    if not cmid:
        return "claimed"
    key = f"idem:{user_id}:{cmid}"
    claimed = await redis.set(key, "processing", nx=True, ex=_IDEM_TTL)
    if claimed:
        return "claimed"
    stored = _decode(await redis.get(key))
    if stored and stored != "processing":
        return stored
    return "processing"


async def _release_idem(redis, user_id: str, cmid: str | None) -> None:
    """轮次失败后释放幂等键。

    认领后一直停留在 "processing"，失败时不清理的话用户在 _IDEM_TTL 内重试，
    只会反复收到"消息正在处理中"，等于把一次超时放大成 5 分钟不可用。
    """
    if not cmid:
        return
    try:
        await redis.delete(f"idem:{user_id}:{cmid}")
    except Exception as e:
        logger.warning(f"幂等键释放失败: {e}")


async def _idem_store_result(redis, user_id: str, cmid: str | None, reply: str, appointment: dict | None) -> None:
    if not cmid:
        return
    try:
        await redis.set(
            f"idem:{user_id}:{cmid}",
            json.dumps({"reply": reply, "appointment": appointment}, ensure_ascii=False),
            ex=_IDEM_TTL,
        )
    except Exception as e:
        logger.warning(f"幂等结果回写失败: {e}")


async def _get_or_create_conversation_id(redis, db: AsyncSession, user_id: str, session_id: str) -> int | None:
    """解析会话对应的 conversations.id，Redis 缓存避免每轮查库。"""
    from sqlalchemy import select
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from src.modules.chat.model import Conversation

    cache_key = f"chat:conv:{user_id}:{session_id}"
    try:
        cached = await redis.get(cache_key)
        if cached:
            return int(cached)
        stmt = pg_insert(Conversation).values(
            user_id=user_id, session_id=session_id, title="",
        ).on_conflict_do_nothing(index_elements=["user_id", "session_id"])
        await db.execute(stmt)
        row = await db.execute(
            select(Conversation.id).where(
                Conversation.user_id == user_id, Conversation.session_id == session_id
            )
        )
        conv_id = row.scalar_one_or_none()
        if conv_id:
            await db.commit()
            await redis.set(cache_key, str(conv_id), ex=86400)
        return conv_id
    except Exception as e:
        logger.warning(f"conversation 解析失败（消息不落库）: {e}")
        return None


_SEQ_TTL_SECONDS = 7 * 86400


async def _ensure_seq_base(redis, user_id: str, session_id: str) -> int:
    """返回会话当前已分配到的最大 seq（必要时用 DB 回填）。

    Redis 里的计数器可能因重启/淘汰丢失；若直接从 0 重来，落库时会与
    uq_chat_message_conv_seq 冲突，被 on_conflict_do_nothing 静默丢弃。
    因此 key 缺失时先查该会话已落库的最大 seq 作为基线。
    """
    key = f"chat:seq:{user_id}:{session_id}"
    try:
        raw = _decode(await redis.get(key))
        if raw:
            return int(raw)
    except Exception as e:
        logger.warning(f"seq 缓存读取失败，尝试用 DB 回填: {e}")

    base = 0
    try:
        from sqlalchemy import func, select
        from src.modules.chat.model import ChatMessage, Conversation

        async with AsyncSessionLocal() as db:
            row = await db.execute(
                select(func.max(ChatMessage.seq))
                .select_from(ChatMessage)
                .join(Conversation, Conversation.id == ChatMessage.conversation_id)
                .where(
                    Conversation.user_id == user_id,
                    Conversation.session_id == session_id,
                )
            )
            base = int(row.scalar() or 0)
    except Exception as e:
        logger.warning(f"seq 回填失败，按 0 起算: {e}")

    try:
        await redis.set(key, str(base), ex=_SEQ_TTL_SECONDS)
    except Exception as e:
        logger.warning(f"seq 基线写回失败: {e}")
    return base


async def _next_seq(redis, user_id: str, session_id: str) -> int:
    """原子分配下一个 seq，并保证计数器带 TTL，不会永久驻留。"""
    await _ensure_seq_base(redis, user_id, session_id)
    key = f"chat:seq:{user_id}:{session_id}"
    seq = int(await redis.incr(key))
    try:
        # INCR 保留已有 TTL，但 key 是本次新建时没有 TTL，这里统一续期
        await redis.expire(key, _SEQ_TTL_SECONDS)
    except Exception:
        pass
    return seq


async def _persist_message(
    redis, user_id: str, session_id: str,
    *, role: str, content: str, event_type: str = "token",
    appointment_data: dict | None = None, turn_id: str = "",
    client_message_id: str | None = None,
    persist: bool = True,
    seq: int | None = None,
) -> int | None:
    """把消息投入批量落库队列，返回 seq。失败不影响主链路。

    seq 由调用方传入时（SSE 单轮内复用同一个分配器）不再逐条访问 Redis；
    未传入时才自增 Redis 计数器。

    persist=False（游客）：只分配 seq（供 SSE id 使用），
    不创建 conversations、不写 chat_messages —— 未登录用户不留任何历史记录。
    """
    from src.core.persist_queue import PersistItem, enqueue_async

    try:
        if seq is None:
            seq = await _next_seq(redis, user_id, session_id)
        if not persist:
            return seq
        async with AsyncSessionLocal() as db:
            conv_id = await _get_or_create_conversation_id(redis, db, user_id, session_id)
        if conv_id is None:
            return None
        # 聊天消息不允许被静默丢弃：队列满时有界等待
        await enqueue_async(PersistItem(kind="chat_message", payload={
            "conversation_id": conv_id,
            "seq": seq,
            "turn_id": turn_id or None,
            "client_message_id": client_message_id,
            "role": role,
            "content": content,
            "event_type": event_type,
            "appointment_data": appointment_data,
        }))
        return seq
    except Exception as e:
        logger.warning(f"消息持久化入队失败（不影响主链路）: {e}")
        return None


# ── 问诊轮次 ──────────────────────────────────────────────────────────────

async def _run_inquiry_turn(
    message: str,
    thread_id: str,
    redis,
    user_id: str = "",
    callbacks: list | None = None,
    persist: bool = True,
    session_id: str = "",
) -> tuple[str, dict | None, bool]:
    """
    执行一轮问诊对话。
    DB 会话短作用域持有：仅包裹 run_inquiry 内真正需要查询的阶段，
    不随 SSE 生命周期占用连接池连接。

    Args:
        persist: False=游客模式，不写问诊记录/预约，状态仅在 Redis（会话级短期记忆）
        session_id: 前端原始会话 ID（与带 user 前缀的 thread_id 区分开）

    Returns:
        (reply, appointment_data | None, is_clarify)
    """
    active_key = f"inquiry_active:{thread_id}"
    state_key  = f"inquiry_state:{thread_id}"

    raw = await redis.get(state_key)
    existing_state = InquiryState.model_validate_json(raw) if raw else None

    # DB 连接只在图执行期间借用，用完立即归还连接池
    async with AsyncSessionLocal() as db:
        deps = await build_inquiry_deps(db_session=db, persist=persist)
        reply, new_state = await run_inquiry(
            user_message=message,
            thread_id=thread_id,
            deps=deps,
            existing_state=existing_state,
            user_id=user_id if persist else None,
            callbacks=callbacks,
            session_id=session_id or None,
        )

    if new_state.phase in (InquiryPhase.HANDOFF, InquiryPhase.END):
        await redis.delete(active_key, state_key)
        if new_state.handoff_payload:
            handoff_reply, appointment_data = await handle_handoff(
                new_state.handoff_payload, persist=persist,
            )
            return f"{reply}\n\n---\n{handoff_reply}", appointment_data, False
        return reply, None, False

    await redis.set(state_key,  new_state.model_dump_json(), ex=3600)
    await redis.set(active_key, "1",                         ex=3600)
    return reply, None, new_state.phase == InquiryPhase.CLARIFY


# ── 非流式接口 ────────────────────────────────────────────────────────────
@router.post("", response_model=ChatResponse)
async def chat(
    req: ChatRequest,
    request: Request,
    current_user: str | None = Depends(get_optional_user_id),
):
    identity = _resolve_identity(current_user, request)
    user_id = identity.user_id
    await enforce_user_message_rate(identity.rate_key)

    # 送模型的副本先脱敏（去身份证/手机号/邮箱/卡号）；落库仍用原文
    llm_message = scrub_for_llm(req.message)

    redis = get_checkpointer_redis()
    thread_id = _thread_id(identity, req.session_id)

    idem = await _idem_check_and_claim(redis, user_id, req.client_message_id)
    if idem == "processing":
        raise BizException(BizErrorCode.CONFLICT, "消息正在处理中，请勿重复提交", status_code=409)
    if idem != "claimed":
        stored = json.loads(idem)
        return ChatResponse(
            reply=stored["reply"], session_id=req.session_id,
            authenticated=identity.authenticated,
        )

    lock = await redis.set(f"lock:turn:{thread_id}", "1", nx=True, px=_LOCK_TTL_MS)
    if not lock:
        raise BizException(BizErrorCode.CONFLICT, "上一条消息仍在处理中，请稍候", status_code=409)

    handler = make_usage_handler(user_id, req.session_id)
    try:
        if await redis.exists(f"inquiry_active:{thread_id}"):
            reply, _appt, _clarify = await guarded(_run_inquiry_turn(
                llm_message, thread_id, redis, user_id=user_id, callbacks=[handler],
                persist=identity.persist, session_id=req.session_id,
            ))
        else:
            agent = await get_supervisor_agent()
            config = {"configurable": {"thread_id": thread_id}, "callbacks": [handler]}
            result = await guarded(agent.ainvoke(
                {"messages": [{"role": "user", "content": llm_message}]},
                config=config,
                context=UserContext(
                    user_id=user_id,
                    session_id=req.session_id,
                    authenticated=identity.authenticated,
                    patient_id=identity.patient_id,
                ),
            ))
            reply = result["messages"][-1].content
            if not isinstance(reply, str):
                reply = str(reply)

        await _persist_message(
            redis, user_id, req.session_id, role="user",
            content=req.message, turn_id=handler.turn_id,
            client_message_id=req.client_message_id, persist=identity.persist,
        )
        await _persist_message(
            redis, user_id, req.session_id, role="assistant",
            content=reply, turn_id=handler.turn_id, persist=identity.persist,
        )
        await _idem_store_result(redis, user_id, req.client_message_id, reply, None)
        return ChatResponse(
            reply=reply, session_id=req.session_id,
            authenticated=identity.authenticated,
        )
    except BizException:
        raise
    except Exception:
        logger.exception("chat 接口异常")
        raise BizException(BizErrorCode.INTERNAL, "服务内部错误，请稍后重试", status_code=500) from None
    finally:
        await redis.delete(f"lock:turn:{thread_id}")


# ── 流式接口（SSE） ───────────────────────────────────────────────────────
@router.post("/stream")
async def chat_stream(
    req: ChatRequest,
    request: Request,
    current_user: str | None = Depends(get_optional_user_id),
):
    identity = _resolve_identity(current_user, request)
    await enforce_user_message_rate(identity.rate_key)

    # 进程内连接数上限（Nginx limit_conn 之外的应用层兜底）
    try:
        await sse_limiter.enter(settings.SSE_MAX_CONNS)
    except SystemBusyError:
        raise BizException(
            BizErrorCode.RATE_LIMITED, "当前在线人数已达上限，请稍后重试",
            status_code=503, headers={"Retry-After": "5"},
        ) from None

    return StreamingResponse(
        _event_generator(req, identity, request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


async def _run_inquiry_turn_events(
    message: str,
    thread_id: str,
    redis,
    user_id: str,
    handler,
    persist: bool,
    queue: asyncio.Queue,
    session_id: str = "",
) -> None:
    """问诊轮次：结果一次性返回，按「追问 / 回复 / 挂号卡片」顺序推入队列。"""
    reply, appointment_data, is_clarify = await guarded(_run_inquiry_turn(
        message, thread_id, redis,
        user_id=user_id, callbacks=[handler],
        persist=persist, session_id=session_id,
    ))
    if is_clarify:
        await queue.put({"type": "clarify", "question": reply})
    await queue.put({"type": "token", "content": reply})
    if appointment_data:
        await queue.put({"type": "appointment", "data": appointment_data})


async def _stream_supervisor_turn_events(
    message: str,
    thread_id: str,
    handler,
    identity: ChatIdentity,
    user_id: str,
    session_id: str,
    queue: asyncio.Queue,
) -> None:
    """Supervisor 轮次：逐 token 推入队列。"""
    agent = await get_supervisor_agent()
    config = {
        "configurable": {"thread_id": thread_id},
        "callbacks": [handler],
    }
    # guarded_stream：整段流式持有 LLM 信号量并限制总耗时，
    # 否则流式主链路会绕过 LLM_MAX_CONCURRENCY 直接打爆上游网关
    async for chunk in guarded_stream(
        agent.astream(
            {"messages": [{"role": "user", "content": message}]},
            config=config,
            stream_mode="messages",
            context=UserContext(
                user_id=user_id,
                session_id=session_id,
                authenticated=identity.authenticated,
                patient_id=identity.patient_id,
            ),
        )
    ):
        if isinstance(chunk, tuple):
            msg_chunk, _ = chunk
            # 只下发 AI 回复：工具调用结果（ToolMessage）、
            # 用户消息回显（HumanMessage）不推给客户端，
            # 否则会看到"没有找到相关记忆"这类内部中间结果
            if not isinstance(msg_chunk, AIMessage):
                continue
            content = msg_chunk.content
            if isinstance(content, str) and content:
                await queue.put({"type": "token", "content": content})


async def _event_generator(req: ChatRequest, identity: ChatIdentity, request: Request):
    """SSE 事件生成器：心跳 + 断连检测 + 总超时 + 任务取消 + 全链路追踪。"""
    redis = get_checkpointer_redis()
    user_id = identity.user_id
    thread_id = _thread_id(identity, req.session_id)
    # 送模型前脱敏；落库与安全审核仍使用原文
    llm_message = scrub_for_llm(req.message)
    queue: asyncio.Queue = asyncio.Queue()
    task: asyncio.Task | None = None
    lock_held = False
    seqs: SseWriter | None = None
    # 追踪 / 预算 / 并发槽：全部在 finally 中释放，异常路径也不漏
    trace = None
    trace_token = None
    budget_token = None
    api_span = None
    slot_key: str | None = None
    outcome = "success"
    error_type: str | None = None

    try:
        yield ": ping\n\n"  # 立即下发首字节，防代理在 LLM 首 token 前掐断

        if settings.DEGRADE_MODE:
            outcome = "degraded"
            yield sse_frame(error_event("degraded", "系统维护中，稍后恢复", retryable=True))
            return

        # 幂等：重复请求直接重放上次结果
        idem = await _idem_check_and_claim(redis, user_id, req.client_message_id)
        if idem == "processing":
            yield sse_frame(error_event("duplicate", "消息正在处理中，请勿重复提交", retryable=False))
            return
        if idem != "claimed":
            stored = json.loads(idem)
            yield sse_frame({"type": "token", "content": stored["reply"]})
            if stored.get("appointment"):
                yield sse_frame({"type": "appointment", "data": stored["appointment"]})
            yield sse_frame({"type": "done", "session_id": req.session_id,
                              "authenticated": identity.authenticated})
            return

        # 同会话互斥锁
        lock_held = await redis.set(f"lock:turn:{thread_id}", "1", nx=True, px=_LOCK_TTL_MS)
        if not lock_held:
            yield sse_frame(error_event("session_busy", "上一条消息仍在处理中，请稍候", retryable=True))
            return

        turn_id = uuid.uuid4().hex
        handler = make_usage_handler(user_id, req.session_id, turn_id=turn_id)

        # seq 在本轮内本地分配：整轮只在进入与结束时各访问一次 Redis。
        # 起点同样走 _ensure_seq_base：计数器丢失时用 DB 最大值兜底，避免重号丢消息。
        seq_key = f"chat:seq:{user_id}:{req.session_id}"
        seqs = SseWriter(
            redis, seq_key, await _ensure_seq_base(redis, user_id, req.session_id),
        )

        # ── 全链路追踪上下文（request_id → trace_id → turn_id） ───────────
        trace, trace_token = start_trace(
            request_id=request_id_var.get(""),
            user_id=user_id,
            session_id=req.session_id,
            turn_id=turn_id,
            entrypoint="/api/v1/chat/stream",
            agent_name="supervisor",
        )
        trace.input_chars = len(req.message)
        # API 请求节点：覆盖整轮 SSE 的生命周期，与内部 supervisor/safety 节点形成父子关系
        api_span = trace.start_span(
            "api.chat_stream", "api",
            attributes={"path": "/api/v1/chat/stream", "session_id": req.session_id},
        )
        _, budget_token = start_budget()

        # 并发任务上限：登录按用户、游客按 IP（跨进程 Redis 计数）
        slot_key = user_id if identity.authenticated else identity.rate_key
        if not await acquire_user_slot(redis, slot_key):
            outcome, error_type = "degraded", "concurrent_limit"
            yield sse_frame(error_event("busy", "您有问诊正在进行中，请稍后再试", retryable=True))
            return

        # ── Safety Engine：输入危险信号审核（命中 critical 直接阻断） ──────
        input_verdict = safety_engine.check_input(req.message)
        if input_verdict.matches or input_verdict.action != "allow":
            record_safety_event(
                input_verdict, stage="input", user_id=user_id,
                session_id=req.session_id, text=req.message,
                request_id=request_id_var.get(""),
            )

        if input_verdict.is_blocking:
            outcome, error_type = "blocked", "safety_input_block"
            reply = input_verdict.reply or "该情况存在紧急医疗风险，请立即线下就医或拨打 120。"
            await _persist_message(
                redis, user_id, req.session_id, role="user",
                content=req.message, turn_id=turn_id,
                client_message_id=req.client_message_id, persist=identity.persist,
                seq=seqs.take(),
            )
            yield sse_frame({"type": "token", "content": reply}, seq=seqs.take())
            yield sse_frame(
                {
                    "type": "safety",
                    "level": input_verdict.level,
                    "notice": input_verdict.primary.advice if input_verdict.primary else "",
                    "rules": input_verdict.rule_ids,
                },
                seq=seqs.take(),
            )
            trace.output_chars = len(reply)
            trace.add_span_snapshot("safety.block", "safety", 0,
                                    action=input_verdict.action, rules=input_verdict.rule_ids)
            await _persist_message(
                redis, user_id, req.session_id, role="assistant",
                content=reply, event_type="safety", turn_id=turn_id,
                persist=identity.persist, seq=seqs.take(),
            )
            await _idem_store_result(redis, user_id, req.client_message_id, reply, None)
            yield sse_frame({
                "type": "done",
                "session_id": req.session_id,
                "authenticated": identity.authenticated,
            })
            return

        async def run_turn():
            """执行一轮对话，把事件推入队列后放哨兵。"""
            try:
                if await redis.exists(f"inquiry_active:{thread_id}"):
                    async with trace_span("consultation_agent", "agent", agent_name="consultation"):
                        await _run_inquiry_turn_events(
                            llm_message, thread_id, redis, user_id, handler,
                            identity.persist, queue, session_id=req.session_id,
                        )
                else:
                    async with trace_span("supervisor_agent", "agent", agent_name="supervisor"):
                        await _stream_supervisor_turn_events(
                            llm_message, thread_id, handler, identity,
                            user_id, req.session_id, queue,
                        )
                await queue.put(None)  # 正常结束哨兵
            except Exception as e:
                await queue.put(_friendly_error(e))
                await queue.put(None)

        task = asyncio.create_task(run_turn())
        deadline = asyncio.get_running_loop().time() + settings.TURN_TIMEOUT

        # 用户消息先落库（含幂等键）；游客 mode 只递增 seq，不落库
        await _persist_message(
            redis, user_id, req.session_id, role="user",
            content=req.message, turn_id=turn_id,
            client_message_id=req.client_message_id, persist=identity.persist,
            seq=seqs.take(),
        )

        full_reply_parts: list[str] = []
        appointment_payload: dict | None = None

        while True:
            try:
                ev = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_INTERVAL)
            except asyncio.TimeoutError:
                if await request.is_disconnected():
                    logger.info(f"客户端断开连接 user={user_id} session={req.session_id}，取消 LLM 任务")
                    outcome, error_type = "cancelled", "client_disconnected"
                    return  # finally 中取消任务
                if asyncio.get_running_loop().time() > deadline:
                    outcome, error_type = "timeout", "turn_timeout"
                    await _release_idem(redis, user_id, req.client_message_id)
                    yield sse_frame(error_event("timeout", "本次回复生成超时，请重试"))
                    return
                yield ": keepalive\n\n"
                continue

            if ev is None:
                break

            if ev.get("type") == "token":
                full_reply_parts.append(ev.get("content", ""))
            if ev.get("type") == "appointment":
                appointment_payload = ev.get("data")
            if ev.get("type") in ("token", "clarify", "appointment"):
                yield sse_frame(ev, seq=seqs.take())
            else:
                # error 事件
                outcome = "error"
                error_type = ev.get("code") or "agent_error"
                yield sse_frame(ev)
                if ev.get("type") == "error":
                    await _release_idem(redis, user_id, req.client_message_id)
                    await _persist_message(
                        redis, user_id, req.session_id, role="assistant",
                        content=ev.get("message", ""), event_type="error", turn_id=turn_id,
                        persist=identity.persist, seq=seqs.take(),
                    )

        # done：输出审核 + 完整回复与 appointment 落库（token 分片不逐条落库）
        full_reply = "".join(full_reply_parts)
        output_verdict = safety_engine.review_output(
            full_reply, emergency=input_verdict.level in ("high", "critical"),
        )
        if output_verdict.matches or output_verdict.action != "allow":
            record_safety_event(
                output_verdict, stage="output", user_id=user_id,
                session_id=req.session_id, text=full_reply,
                request_id=request_id_var.get(""),
            )
        final_reply = safety_engine.finalize_output(full_reply, output_verdict)
        if final_reply != full_reply:
            # 输出被纠正/阻断：让前端替换已渲染的正文
            yield sse_frame(
                {
                    "type": "safety",
                    "level": output_verdict.level,
                    "notice": output_verdict.primary.advice if output_verdict.primary else "",
                    "rules": output_verdict.rule_ids,
                    "content": final_reply,
                },
                seq=seqs.take(),
            )

        # Token 预算超限只降级记录，不丢弃已生成内容
        budget = current_budget()
        if budget is not None:
            try:
                budget.ensure_tokens()
            except BudgetExceeded as e:
                outcome, error_type = "degraded", e.kind
                logger.warning(f"Token 预算超限 user={user_id} turn={turn_id}: {budget.tokens}")

        trace.output_chars = len(final_reply)
        trace.add_span_snapshot(
            "final_response", "final_response", 0,
            output_chars=len(final_reply), safety_action=output_verdict.action,
        )

        await _persist_message(
            redis, user_id, req.session_id, role="assistant",
            content=final_reply, turn_id=turn_id, persist=identity.persist,
            seq=seqs.take(),
        )
        if appointment_payload:
            await _persist_message(
                redis, user_id, req.session_id, role="assistant",
                content="", event_type="appointment",
                appointment_data=appointment_payload, turn_id=turn_id,
                persist=identity.persist, seq=seqs.take(),
            )
        await _idem_store_result(redis, user_id, req.client_message_id, final_reply, appointment_payload)
        yield sse_frame({
            "type": "done",
            "session_id": req.session_id,
            "authenticated": identity.authenticated,
        })

    except asyncio.CancelledError:
        outcome, error_type = "cancelled", "cancelled"
        raise
    except Exception as e:
        outcome, error_type = "error", type(e).__name__
        logger.exception("chat/stream 异常")
        await _release_idem(redis, user_id, req.client_message_id)
        yield sse_frame(error_event("internal", "服务暂时不可用，请稍后重试"))
    finally:
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        if lock_held:
            try:
                await redis.delete(f"lock:turn:{thread_id}")
            except Exception:
                pass
        if seqs is not None:
            await seqs.sync()
        if slot_key is not None:
            await release_user_slot(redis, slot_key)
        reset_budget(budget_token)
        if trace is not None:
            if api_span is not None:
                trace.end_span(
                    api_span,
                    status="ok" if outcome in ("success", "blocked") else "error",
                    error_type=error_type,
                )
            end_trace(trace, trace_token, status=outcome, error_type=error_type)
        await sse_limiter.exit()


# ── 断线续传：拉取未送达事件 ───────────────────────────────────────────────
@router.get("/stream/pending")
async def pending_events(
    session_id: str,
    after_seq: int = 0,
    current_user: str = Depends(get_current_user_id),
):
    """按 seq 增量拉取已落库但客户端可能未收到的事件（断线重连恢复用）。"""
    from sqlalchemy import select
    from src.modules.chat.model import Conversation, ChatMessage

    user_id = current_user
    try:
        async with AsyncSessionLocal() as db:
            conv = (await db.execute(
                select(Conversation).where(
                    Conversation.user_id == user_id,
                    Conversation.session_id == session_id,
                )
            )).scalar_one_or_none()
            if conv is None:
                return {"events": [], "has_more": False}
            rows = (await db.execute(
                select(ChatMessage)
                .where(ChatMessage.conversation_id == conv.id, ChatMessage.seq > after_seq)
                .order_by(ChatMessage.seq.asc())
                .limit(200)
            )).scalars().all()
            events = []
            for m in rows:
                # 带上 role：前端重连时才知道这条事件是用户提问还是助手回答
                ev: dict = {"type": m.event_type or "token", "seq": m.seq, "role": m.role}
                if m.event_type == "appointment":
                    ev["data"] = m.appointment_data
                else:
                    ev["content"] = m.content
                events.append(ev)
            return {"events": events, "has_more": len(rows) == 200}
    except Exception:
        logger.exception("pending 事件查询失败")
        raise BizException(BizErrorCode.INTERNAL, "服务内部错误，请稍后重试", status_code=500) from None
