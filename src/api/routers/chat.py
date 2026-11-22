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
from langchain_core.messages import AIMessage, HumanMessage
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
from src.agents.workers.inquiry_agent import judge_appointment_confirmation, handle_handoff
from src.modules.clinical.triage import gate as triage_gate
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
from src.core.persist_queue import PersistItem, enqueue
from src.core.redaction import redact_phi
from src.core.tracing import end_trace, start_trace, trace_span
from src.api.rate_limit import enforce_user_message_rate
from src.api.deps import (
    ANON_USER_ID,
    AuthContext,
    get_current_user_id,
    get_optional_auth_context,
    optional_require_scope,
)
from src.api.routers.sse import SseWriter, error_event, sse_frame
from src.modules.clinical.link import forget_pending
from src.modules.tenant.model import DEFAULT_TENANT_ID
from src.core.config import get_settings

settings = get_settings()
router = APIRouter(
    prefix="/api/v1/chat", tags=["chat"],
    # 问诊属于"写"操作：匿名放行（游客模式不落库），已登录但缺 chat:write 的角色
    # （如只有 chat:read 的护士）一律 403 —— 避免登录身份被当成游客继续用。
    dependencies=[Depends(optional_require_scope("chat:write"))],
)

HEARTBEAT_INTERVAL = 15.0          # SSE 心跳间隔（秒），须小于 Nginx 读超时
# 同会话互斥锁 TTL：必须覆盖整轮耗时。此前固定 30s，长轮次进行中锁会先过期，
# 用户补发的消息得以并发跑在同一个 thread_id 上，互相覆盖 checkpoint
_LOCK_TTL_MS = int(settings.TURN_TIMEOUT * 1000) + 30_000
# 出结论后等患者确认"要不要预约"的窗口期：超时未表态就丢弃待办，不再建预约
CONFIRM_TTL_SECONDS = 600
_IDEM_TTL = 300                    # 幂等键保留窗口（秒）


class ChatRequest(BaseModel):
    session_id: str
    message: str
    patient_id: int | None = None
    # 图片问答：患者可随消息上传一张图片（data URL 或裸 base64）。
    # 不落库、不进日志，仅在本轮内存中交给视觉模型转成文字描述后拼进对话。
    image_base64: str | None = Field(
        default=None,
        max_length=12_000_000,
        description="图片内容（data URL 或裸 base64），前端已压到长边 1600px",
    )
    image_name: str | None = Field(
        default=None, max_length=120, description="原始文件名，仅用于日志与诊断",
    )
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
    # C1：会话/消息必须写到患者所属租户。游客不落库，默认租户即可
    tenant_id: int = DEFAULT_TENANT_ID

    @property
    def persist(self) -> bool:
        """是否允许写持久化数据（会话、消息、问诊记录、预约）。"""
        return self.authenticated


def _resolve_identity(ctx: AuthContext | None, request: Request) -> ChatIdentity:
    """解析请求身份：带合法 Token 为登录用户，否则按游客（IP 维度限流）。

    Token 非法/过期由 get_optional_auth_context 抛 401，不会静默降级为游客，
    避免"登录态失效"被误当成游客而丢失历史。
    """
    if ctx is not None:
        try:
            patient_id: int | None = int(ctx.subject_id)
        except (TypeError, ValueError):
            patient_id = None
        return ChatIdentity(
            user_id=ctx.subject_id,
            authenticated=True,
            rate_key=f"user:{ctx.subject_id}",
            patient_id=patient_id,
            tenant_id=ctx.tenant_id,
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


async def _get_or_create_conversation_id(
    redis, db: AsyncSession, user_id: str, session_id: str, tenant_id: int = DEFAULT_TENANT_ID,
) -> int | None:
    """解析会话对应的 conversations.id，Redis 缓存避免每轮查库。

    C1：新会话显式写 tenant_id。不写的话会吃列默认值 1，
    租户 2 的患者聊完天，历史记录全落到默认租户。
    缓存键带租户，避免同 user_id 跨租户命中同一条会话。
    """
    from sqlalchemy import select
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from src.modules.chat.model import Conversation

    cache_key = f"chat:conv:{int(tenant_id)}:{user_id}:{session_id}"
    try:
        cached = await redis.get(cache_key)
        if cached:
            return int(cached)
        stmt = pg_insert(Conversation).values(
            tenant_id=int(tenant_id), user_id=user_id, session_id=session_id, title="",
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
from src.modules.chat.seq_allocator import ensure_seq_base as _ensure_seq_base, next_seq as _next_seq


def _citation_item(raw: dict) -> dict:
    """把检索元信息转成对外引用项。

    page/chunk/score 可能是 numpy 或 int64，直接塞进 JSONB 会序列化失败，
    统一在此归一为原生类型；文本字段按表宽度截断。
    """
    def _int(v):
        try:
            return int(v) if v is not None and v != "" else None
        except (TypeError, ValueError):
            return None

    def _float(v):
        try:
            return float(v) if v is not None and v != "" else None
        except (TypeError, ValueError):
            return None

    return {
        "doc_name": str(raw.get("doc_name") or raw.get("title") or "")[:256],
        "doc_type": str(raw.get("doc_type") or "")[:40],
        "engine": str(raw.get("engine") or "")[:24],
        "page_number": _int(raw.get("page_number")),
        "chunk_index": _int(raw.get("chunk_index")),
        "score": _float(raw.get("score")),
        "score_type": str(raw.get("score_type") or "vector")[:16],
        "quote": str(raw.get("quote") or "")[:400],
    }


async def _persist_message(
    redis, identity: ChatIdentity, session_id: str,
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

    身份用 ChatIdentity 而不是裸 user_id：tenant_id 必须跟着一起传下去，
    否则会话/消息会落到默认租户。
    """
    from src.core.persist_queue import PersistItem, enqueue_async

    user_id = identity.user_id
    try:
        if seq is None:
            seq = await _next_seq(redis, user_id, session_id)
        if not persist:
            return seq
        async with AsyncSessionLocal() as db:
            conv_id = await _get_or_create_conversation_id(
                redis, db, user_id, session_id, identity.tenant_id,
            )
        if conv_id is None:
            return None
        # 聊天消息不允许被静默丢弃：队列满时有界等待
        await enqueue_async(PersistItem(kind="chat_message", payload={
            "conversation_id": conv_id,
            "tenant_id": int(identity.tenant_id),
            "seq": seq,
            "turn_id": turn_id or None,
            "client_message_id": client_message_id,
            "role": role,
            "content": content,
            # 原文照存（临床审计追溯），同时存一份去 PHI 的副本供对外展示；
            # 管理端列表、导出、语料复用一律只读这一列
            "content_redacted": redact_phi(content),
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
    tenant_id: int = 1,
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
        deps = await build_inquiry_deps(
            db_session=db, persist=persist, tenant_id=tenant_id,
        )
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
        # 出结论不等于要挂号。这里不再直接 handle_handoff（那会立刻建一条预约），
        # 而是把结论 + "要不要预约"的问句返回给患者，并把状态留在 Redis：
        # 患者下一轮明确说"要"，才走预约；说"不用"，就只关掉状态。
        await redis.delete(active_key)
        if new_state.handoff_payload:
            await redis.set(state_key, new_state.model_dump_json(), ex=CONFIRM_TTL_SECONDS)
            ask = (
                "请问是否现在帮您预约挂号？回复「需要」即可预约，回复「不用」就不预约。"
            )
            tail = "" if "预约" in reply and "？" in reply.split("预约")[-1][:8] else f"\n\n{ask}"
            return f"{reply}{tail}", None, False
        # 没有移交数据包（如急症直接结束）：清状态，本次对话到此为止
        await redis.delete(state_key)
        return reply, None, False

    await redis.set(state_key,  new_state.model_dump_json(), ex=3600)
    await redis.set(active_key, "1",                         ex=3600)
    return reply, None, new_state.phase == InquiryPhase.CLARIFY


# ── 非流式接口 ────────────────────────────────────────────────────────────
@router.post("", response_model=ChatResponse)
async def chat(
    req: ChatRequest,
    request: Request,
    ctx: AuthContext | None = Depends(get_optional_auth_context),
):
    identity = _resolve_identity(ctx, request)
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

    handler = make_usage_handler(user_id, req.session_id, tenant_id=identity.tenant_id)
    try:
        # B3：急症闸门优先于一切业务判定 —— 红旗症状先分级落库，直接给急诊指引
        _emergency = await _emergency_gate(
            llm_message,
            identity=identity,
            user_id=user_id,
            session_id=req.session_id,
            thread_id=thread_id,
        )
        if _emergency:
            reply = _emergency["reply"]
            await redis.delete(f"inquiry_state:{thread_id}")
            await _idem_store_result(redis, user_id, req.client_message_id, reply, None)
            return ChatResponse(
                reply=reply, session_id=req.session_id,
                authenticated=identity.authenticated,
            )

        # B2：优先看 clinical_actions 里的待确认动作；没有则回退旧的 Redis 意向
        _pending_info = await _load_pending_action(thread_id)
        action_id = _pending_info[0] if _pending_info else None
        pending = None if action_id else await _pending_appointment(redis, thread_id)
        if action_id is not None or pending is not None:
            answer = await judge_appointment_confirmation(llm_message)
            if answer is True:
                if action_id is not None:
                    reply, _appt = await _approve_pending_action(action_id, user_id)
                    await forget_pending(thread_id)
                elif identity.persist:
                    reply, _appt = await _approve_legacy_handoff(
                        pending, user_id, thread_id, req.session_id,
                        tenant_id=identity.tenant_id,
                    )
                else:
                    reply, _appt = await handle_handoff(pending, persist=False)
                await redis.delete(f"inquiry_state:{thread_id}")
            elif answer is False:
                if action_id is not None:
                    await _reject_pending_action(action_id, user_id, "患者在对话中明确拒绝")
                    await forget_pending(thread_id)
                await redis.delete(f"inquiry_state:{thread_id}")
                reply = "好的，这次先不预约。如果之后想挂号，随时告诉我。"
            elif await redis.exists(f"inquiry_active:{thread_id}"):
                reply, _appt, _clarify = await guarded(_run_inquiry_turn(
                    llm_message, thread_id, redis, user_id=user_id, callbacks=[handler],
                    persist=identity.persist, session_id=req.session_id,
                    tenant_id=identity.tenant_id,
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
                        tenant_id=identity.tenant_id,
                    ),
                ))
                reply = result["messages"][-1].content
                if not isinstance(reply, str):
                    reply = str(reply)
        elif await redis.exists(f"inquiry_active:{thread_id}"):
            reply, _appt, _clarify = await guarded(_run_inquiry_turn(
                llm_message, thread_id, redis, user_id=user_id, callbacks=[handler],
                persist=identity.persist, session_id=req.session_id,
                tenant_id=identity.tenant_id,
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
                    tenant_id=identity.tenant_id,
                ),
            ))
            reply = result["messages"][-1].content
            if not isinstance(reply, str):
                reply = str(reply)

        await _persist_message(
            redis, identity, req.session_id, role="user",
            content=req.message, turn_id=handler.turn_id,
            client_message_id=req.client_message_id, persist=identity.persist,
        )
        await _persist_message(
            redis, identity, req.session_id, role="assistant",
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
    ctx: AuthContext | None = Depends(get_optional_auth_context),
):
    identity = _resolve_identity(ctx, request)
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


async def _pending_appointment(redis, thread_id: str):
    """有没有"已出结论、等患者点头"的挂号待办。

    出结论时状态以 phase=HANDOFF + handoff_payload 留在 Redis，
    患者下一轮说"要"才真正建预约；超时（CONFIRM_TTL_SECONDS）自动失效。
    """
    raw = await redis.get(f"inquiry_state:{thread_id}")
    if not raw:
        return None
    try:
        state = InquiryState.model_validate_json(raw)
    except Exception:
        return None
    # 注意：node_save_record 会把 phase 从 HANDOFF 改成 END，但 handoff_payload 保留，
    # 所以两个阶段都要认，否则"等确认"的待办会被漏掉。
    if state.phase in (InquiryPhase.HANDOFF, InquiryPhase.END) and state.handoff_payload:
        return state.handoff_payload
    return None


# ── B2：临床动作确认（clinical_actions 状态机） ─────────────────────────────
async def _load_pending_action(thread_id: str) -> tuple[int, dict, str] | None:
    """找出本会话待患者确认的动作，返回 (action_id, 对外结构, action_type)。

    B2 起"等患者确认"的权威状态在 clinical_actions 表，Redis 里只有一个指针。
    指针丢失/过期时返回 None，调用方回退到旧的 Redis-only 判定。
    """
    from src.infra.database import AsyncSessionLocal
    from src.modules.clinical import actions as clinical
    from src.modules.clinical.link import read_pending

    action_id = await read_pending(thread_id)
    if not action_id:
        return None
    try:
        async with AsyncSessionLocal() as db:
            action = await clinical.get_action(db, action_id)
            if action.status not in ("pending_confirm", "draft"):
                return None
            return action_id, clinical.public_dict(action), action.action_type
    except Exception as e:  # noqa: BLE001 —— 读不到就当没有待确认，不影响本轮对话
        logger.warning(f"待确认动作读取失败 id={action_id}: {type(e).__name__}: {e}")
        return None


async def _pending_confirm_payload(thread_id: str) -> dict | None:
    info = await _load_pending_action(thread_id)
    return info[1] if info else None


async def _session_ticket(user_id: str, session_id: str) -> dict | None:
    """本会话当前的人工工单（序列化后的快照；没有则为 None）。

    用快照而不是 ORM 对象：helper 里的 session 用完就关，返回 ORM 实例容易踩
    detached 属性的坑；工单在这里只读展示，不需要 ORM 语义。
    """
    if not user_id.isdigit():
        return None
    try:
        from src.infra.database import AsyncSessionLocal
        from src.modules.handoff import service as handoff_service

        async with AsyncSessionLocal() as db:
            ticket = await handoff_service.active_ticket(
                db, patient_id=int(user_id), session_id=session_id,
            )
            return handoff_service.serialize(ticket) if ticket else None
    except Exception as e:  # noqa: BLE001 —— 工单查询失败不该拖垮整轮对话
        logger.warning(f"会话工单查询失败 user={user_id} session={session_id}: {type(e).__name__}: {e}")
        return None


async def _max_session_ticket_id(
    user_id: str, session_id: str, tenant_id: int = 1,
) -> int:
    """本轮开始前该会话的最大工单 id（用来判断"这一轮是否新建了工单"）。

    为什么不用时间比较：`created_at` 是 DB 的 `func.now()`（容器 UTC），
    而 Python 的 `datetime.now()` 是本地时间（UTC+8），直接比会把 8 小时前的单
    当成"刚建的单"。用 id 单调递增判断，不受时区影响。
    """
    if not user_id.isdigit():
        return 0
    try:
        from sqlalchemy import func, select

        from src.infra.database import AsyncSessionLocal
        from src.modules.handoff.model import HandoffTicket

        async with AsyncSessionLocal() as db:
            return int((await db.execute(
                select(func.max(HandoffTicket.id)).where(
                    HandoffTicket.tenant_id == tenant_id,
                    HandoffTicket.patient_id == int(user_id),
                    HandoffTicket.session_id == session_id,
                )
            )).scalar_one_or_none() or 0)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"工单 max_id 查询失败: {type(e).__name__}: {e}")
        return 0


async def _emergency_gate(
    text: str,
    *,
    identity,
    user_id: str,
    session_id: str,
    thread_id: str,
) -> dict | None:
    """B3 急症闸门：supervisor 之前的最后一道安全前置。

    supervisor 的提示词要求它自己答急症，那样记录只停在文案里、库里为零；
    这里先分级落库再回指引，保证"患者看到急诊提示"和"库里有一条分级记录"同时成立。
    """
    try:
        from src.core.tracing import get_trace_id

        trace_id = get_trace_id()
    except Exception:  # noqa: BLE001 —— 追踪不可用不影响分级
        trace_id = ""

    patient_id = int(user_id) if str(user_id).isdigit() else None
    return await triage_gate(
        text,
        patient_id=patient_id,
        user_id=user_id,
        session_id=session_id,
        thread_id=thread_id,
        trace_id=trace_id,
        persist=identity.persist,
        tenant_id=identity.tenant_id,
    )


def _approved_reply(data: dict, ref: str) -> str:
    appointment_no = data.get("appointment_no") or ref
    if not appointment_no:
        return "已确认，操作已完成。"
    return (
        f"已为您成功预约挂号！\n\n"
        f"预约号：{appointment_no}\n"
        f"就诊科室：{data.get('department', '')}\n"
        f"就诊医生：{data.get('doctor_name', '')}\n"
        f"就诊时间：{data.get('date', '')} {data.get('time_slot', '')}\n"
        f"就诊序号：第 {data.get('queue_number', 0)} 号\n"
        f"挂号费：{data.get('fee', 0)} 元\n\n"
        f"请提前 15 分钟到诊室门口等候，祝您早日康复。"
    )


async def _approve_pending_action(action_id: int, user_id: str) -> tuple[str, dict]:
    """患者确认 → 状态机批准 → 执行器落库。返回 (文案, 预约数据)。"""
    from src.infra.database import AsyncSessionLocal
    from src.modules.clinical import actions as clinical
    from src.modules.clinical.executors import executor_for

    async with AsyncSessionLocal() as db:
        action = await clinical.get_action(db, action_id)
        executed, data = await clinical.approve(
            db, action_id,
            actor=f"patient:{user_id}",
            executor=executor_for(action.action_type),
        )
        ref = executed.result_ref
    return _approved_reply(data, ref), data


async def _reject_pending_action(action_id: int, user_id: str, reason: str) -> None:
    from src.infra.database import AsyncSessionLocal
    from src.modules.clinical import actions as clinical

    async with AsyncSessionLocal() as db:
        await clinical.reject(db, action_id, actor=f"patient:{user_id}", reason=reason)


async def _approve_legacy_handoff(
    payload: dict, user_id: str, thread_id: str, session_id: str,
    tenant_id: int = 1,
) -> tuple[str, dict]:
    """B2 收口：历史 Redis 意向（没有 clinical_action）也走状态机落库。

    这条分支原来直接调 handle_handoff 写一条 confirmed 预约，是状态机之外的第二条
    落库入口。现在改成「建 handoff 草稿 → 立即批准」，落库、审计、幂等与卡片路径一致。
    """
    from src.infra.database import AsyncSessionLocal
    from src.modules.clinical import actions as clinical
    from src.modules.clinical.executors import executor_for
    from src.modules.clinical.model import ACTION_HANDOFF, SOURCE_AGENT

    patient_id = int(user_id) if str(user_id).isdigit() else None
    async with AsyncSessionLocal() as db:
        row = await clinical.create_action(
            db,
            action_type=ACTION_HANDOFF,
            payload={"handoff_payload": payload},
            summary="问诊结论转预约（历史意向）",
            patient_id=patient_id,
            user_id=user_id,
            session_id=session_id,
            thread_id=thread_id,
            source=SOURCE_AGENT,
            requested_by="chat:legacy_handoff",
            tenant_id=tenant_id,
        )
        executed, data = await clinical.approve(
            db, row.id, actor=f"patient:{user_id}", executor=executor_for(ACTION_HANDOFF),
        )
    return _approved_reply(data, executed.result_ref), data


async def _run_inquiry_turn_events(
    message: str,
    thread_id: str,
    redis,
    user_id: str,
    handler,
    persist: bool,
    queue: asyncio.Queue,
    session_id: str = "",
    tenant_id: int = 1,
) -> None:
    """问诊轮次：结果一次性返回，按「追问 / 回复 / 挂号卡片」顺序推入队列。"""
    reply, appointment_data, is_clarify = await guarded(_run_inquiry_turn(
        message, thread_id, redis,
        user_id=user_id, callbacks=[handler],
        persist=persist, session_id=session_id, tenant_id=tenant_id,
    ))
    if is_clarify:
        await queue.put({"type": "clarify", "question": reply})
    await queue.put({"type": "token", "content": reply})
    if appointment_data:
        await queue.put({"type": "appointment", "data": appointment_data})
    # 问诊轮次走的是 inquiry 图，不经过 supervisor，因此 supervisor 的 checkpoint
    # 里缺这几轮对话——下一轮用户说"还是没有好转"时，supervisor 手上没有前文，
    # 就会当成一次全新的模糊描述，重新开问诊并反问"哪个部位不舒服"。
    # 这里把本轮的问答补写回 supervisor 的历史，保证后续轮次能看到完整上下文。
    try:
        agent = await get_supervisor_agent()
        await agent.aupdate_state(
            {"configurable": {"thread_id": thread_id}},
            {
                "messages": [
                    HumanMessage(content=message),
                    AIMessage(content=reply),
                ]
            },
        )
    except Exception as e:
        logger.warning(f"问诊轮次回写 supervisor 历史失败（不影响本轮回答）: "
                       f"{type(e).__name__}: {e}")


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
    json_guard: dict[str, bool] = {}
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
                tenant_id=identity.tenant_id,
            ),
        )
    ):
        if isinstance(chunk, tuple):
            msg_chunk, meta = chunk
            # 会话压缩中间件会用同一个模型跑一段摘要提示词，产物也是 AIMessage。
            # 这类内部调用带了 summarization 标签，必须剔除，否则用户会看到
            # "## SESSION INTENT / SUMMARY / ARTIFACTS" 之类的内部模板文本。
            if "summarization" in ((meta or {}).get("tags") or []):
                continue
            # 子 Agent 是以"工具"的形式挂在 supervisor 上的，它内部的 LLM 调用同样会
            # 出现在 stream_mode="messages" 里，并且节点名也是 model。这类 chunk 带
            # lc_agent_name（如 report_agent），要整条剔除：它的正文只是 supervisor
            # 的中间素材，推出去会和 supervisor 的最终回答叠成"同一答案两遍"。
            # 实测证据：子 Agent 的 chunk thread_id 还多一层 "user_id:" 前缀，
            # 但主 Agent 未设 name、子 Agent 必有 name，用 lc_agent_name 判断更稳。
            if (meta or {}).get("lc_agent_name"):
                continue
            # 只下发 AI 回复：工具调用结果（ToolMessage）、
            # 用户消息回显（HumanMessage）不推给客户端，
            # 否则会看到"没有找到相关记忆"这类内部中间结果
            if not isinstance(msg_chunk, AIMessage):
                continue
            content = msg_chunk.content
            if not isinstance(content, str) or not content:
                continue
            # 模型偶尔会在真正调工具前先"预演"一段 JSON（挂号参数、症状数组等），
            # 这段同样是 AIMessage 的 content，直接推出去用户会看到裸 JSON。
            # 一条消息一旦以 { 或 ```json 开头，就整条抑制，直到该消息结束。
            mid = getattr(msg_chunk, "id", "") or ""
            if mid not in json_guard:
                json_guard[mid] = content.lstrip().startswith(("{", "```json"))
            if json_guard[mid]:
                continue
            await queue.put({"type": "token", "content": content})


async def _run_cleanup(make_coro) -> None:
    """在已被取消的任务里把清理动作跑完。

    finally 里直接 await 会被 CancelledError 立即打断（它是 BaseException，
    except Exception 拦不住）。shield 让内层协程挂到独立 task 上继续执行，
    外层这次取消只作用于 await 本身，因此这里用 BaseException 兜底吞掉。
    传入 callable 而不是现成的协程：避免"coroutine was never awaited"。
    """
    try:
        await asyncio.shield(make_coro())
    except BaseException:
        pass


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
    # acquire 失败时内部已经 decr 回退，这里必须区分"是否真的拿到槽"，
    # 否则 finally 会再减一次，把同用户另一个在跑请求的名额偷掉
    slot_acquired = False
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
        handler = make_usage_handler(
            user_id, req.session_id, turn_id=turn_id, tenant_id=identity.tenant_id,
        )

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
        slot_acquired = True

        # ── 图片问答：先把图片转成文字，再交给纯文本 Agent ────────────────
        # 主干 Agent 绑的是纯文本 chat 模型，不能直接吃 image；
        # 转文字后图片内容会作为普通文本参与安全审核、检索与多轮上下文。
        image_desc = ""
        if req.image_base64:
            yield sse_frame({"type": "status", "content": "正在识别图片…"}, seq=seqs.take())
            from src.infra.vl_ocr import describe_medical_image

            image_desc = await describe_medical_image(
                req.image_base64, req.image_name or "", req.message,
            )
            if image_desc:
                llm_message = (
                    f"{llm_message}\n\n"
                    f"【患者随消息上传了一张图片，以下是对图片内容的客观描述】\n{image_desc}"
                )
            else:
                llm_message = (
                    f"{llm_message}\n\n"
                    "（患者随消息上传了一张图片，但系统未能识别出图中内容。"
                    "请在回答中说明没看清，并请患者改用文字描述或重传更清晰的图片。）"
                )
            trace.add_span_snapshot(
                "image_qa", "tool", 0,
                understood=bool(image_desc), chars=len(image_desc),
            )

        # ── Safety Engine：输入危险信号审核（命中 critical 直接阻断） ──────
        # 图片理解结果一并送审：图片里写着什么，模型转述后同样可能带危险信号
        audit_text = f"{req.message}\n{image_desc}" if image_desc else req.message
        input_verdict = safety_engine.check_input(audit_text)
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
                redis, identity, req.session_id, role="user",
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
                redis, identity, req.session_id, role="assistant",
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

        # ── B3：急症闸门（supervisor 之前）──────────────────────────────
        # 放在 safety 之后：先做合规审核，再做临床分级；两者都不走 supervisor。
        _emergency = await _emergency_gate(
            llm_message,
            identity=identity,
            user_id=user_id,
            session_id=req.session_id,
            thread_id=thread_id,
        )
        if _emergency:
            reply = _emergency["reply"]
            await _persist_message(
                redis, identity, req.session_id, role="user",
                content=req.message, turn_id=turn_id,
                client_message_id=req.client_message_id, persist=identity.persist,
                seq=seqs.take(),
            )
            await redis.delete(f"inquiry_state:{thread_id}")
            yield sse_frame({"type": "token", "content": reply}, seq=seqs.take())
            trace.output_chars = len(reply)
            trace.add_span_snapshot(
                "triage", "safety", 0,
                level=_emergency["level"], reason=_emergency["reason"],
            )
            await _persist_message(
                redis, identity, req.session_id, role="assistant",
                content=reply, turn_id=turn_id,
                persist=identity.persist, seq=seqs.take(),
            )
            await _idem_store_result(redis, user_id, req.client_message_id, reply, None)
            outcome, error_type = "emergency_triage", "none"
            yield sse_frame({
                "type": "done",
                "session_id": req.session_id,
                "authenticated": identity.authenticated,
            })
            return

        async def run_turn():
            """执行一轮对话，把事件推入队列后放哨兵。"""
            try:
                # C3：坐席已认领 → AI 让位。患者这条消息已经落库（role=user），
                # 坐席通过 /tickets/{id}/messages 读到的就是它，AI 不再抢答。
                _claimed = await _session_ticket(user_id, req.session_id)
                if _claimed and _claimed.get("status") == "claimed" and identity.persist:
                    who = _claimed.get("assigned_name") or f"坐席#{_claimed.get('assigned_to')}"
                    await queue.put({
                        "type": "token",
                        "content": (
                            f"你的问题已转给人工坐席（{who}，工单 {_claimed.get('ticket_no')}），"
                            "坐席看到后会直接在这里回复你。"
                        ),
                    })
                    await queue.put({"type": "handoff", "data": _claimed})
                    await queue.put(None)
                    return

                # B2：优先 clinical_actions；没有则回退旧的 Redis 意向
                _pending_info = await _load_pending_action(thread_id)
                confirm_action_id = _pending_info[0] if _pending_info else None
                pending = None if confirm_action_id else await _pending_appointment(redis, thread_id)
                if confirm_action_id is not None or pending is not None:
                    answer = await judge_appointment_confirmation(llm_message)
                    if answer is True:
                        # 患者明确同意：这一步才真正建预约
                        if confirm_action_id is not None:
                            handoff_reply, appointment_data = await _approve_pending_action(
                                confirm_action_id, user_id,
                            )
                            await forget_pending(thread_id)
                        elif identity.persist:
                            handoff_reply, appointment_data = await _approve_legacy_handoff(
                                pending, user_id, thread_id, req.session_id,
                                tenant_id=identity.tenant_id,
                            )
                        else:
                            handoff_reply, appointment_data = await handle_handoff(
                                pending, persist=False,
                            )
                        await redis.delete(f"inquiry_state:{thread_id}")
                        await queue.put({"type": "token", "content": handoff_reply})
                        if appointment_data:
                            await queue.put({"type": "appointment", "data": appointment_data})
                        await queue.put(None)
                        return
                    if answer is False:
                        if confirm_action_id is not None:
                            await _reject_pending_action(
                                confirm_action_id, user_id, "患者在对话中明确拒绝",
                            )
                            await forget_pending(thread_id)
                        await redis.delete(f"inquiry_state:{thread_id}")
                        await queue.put({
                            "type": "token",
                            "content": "好的，这次先不预约。如果之后想挂号，随时告诉我。",
                        })
                        await queue.put(None)
                        return
                    # 既没答应也没拒绝（比如接着问注意事项）：不动状态，正常对话
                if await redis.exists(f"inquiry_active:{thread_id}"):
                    async with trace_span("consultation_agent", "agent", agent_name="consultation"):
                        await _run_inquiry_turn_events(
                            llm_message, thread_id, redis, user_id, handler,
                            identity.persist, queue, session_id=req.session_id,
                            tenant_id=identity.tenant_id,
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
        # 判断"本轮是否新建了工单"的基线：本轮开始前的最大工单 id
        ticket_id_before = await _max_session_ticket_id(
        user_id, req.session_id, tenant_id=identity.tenant_id,
    )

        # 用户消息先落库（含幂等键）；游客 mode 只递增 seq，不落库
        await _persist_message(
            redis, identity, req.session_id, role="user",
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
            if ev.get("type") in ("token", "clarify", "appointment", "handoff"):
                yield sse_frame(ev, seq=seqs.take())
            else:
                # error 事件
                outcome = "error"
                error_type = ev.get("code") or "agent_error"
                yield sse_frame(ev)
                if ev.get("type") == "error":
                    await _release_idem(redis, user_id, req.client_message_id)
                    await _persist_message(
                        redis, identity, req.session_id, role="assistant",
                        content=ev.get("message", ""), event_type="error", turn_id=turn_id,
                        persist=identity.persist, seq=seqs.take(),
                    )

        # ── C3：本轮刚转人工 → 补一句告知 + handoff 事件 ────────────────
        # 挂起后模型不会再产出正文（interrupt 在 after_model），患者侧必须由这里
        # 补一条"已转人工 + 工单号"，否则用户看到的是一轮什么都没有的空回复。
        handoff_view = await _session_ticket(user_id, req.session_id)
        if (handoff_view and identity.persist
                and int(handoff_view.get("id") or 0) > ticket_id_before):
            if not "".join(full_reply_parts).strip():
                note = (
                    f"已为你转接人工坐席（工单 {handoff_view.get('ticket_no')}）。"
                    "坐席接入前你可以继续补充情况，我会一并交给坐席；"
                    "坐席回复后会直接显示在这个会话里。"
                )
                full_reply_parts.append(note)
                yield sse_frame({"type": "token", "content": note}, seq=seqs.take())
            yield sse_frame({"type": "handoff", "data": handoff_view}, seq=seqs.take())
            trace.add_span_snapshot(
                "handoff", "handoff", 0,
                ticket_no=handoff_view.get("ticket_no"),
                priority=handoff_view.get("priority"),
                reason=handoff_view.get("reason"),
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

        # ── 引用来源：把本轮命中的知识片段下发给前端，并落库留痕 ──────────
        # 数据源是 TraceRecorder.add_retrieval 收集的检索元信息（doc_rag / graph_rag
        # 都在命中时写入），因此这里不需要另开一条采集通道，避免两套引用数据互相打架。
        cites = [
            c for c in (trace.retrieval_sources or [])
            if c.get("doc_name") or c.get("doc_id")
        ]
        if cites:
            items = [_citation_item(c) for c in cites[:8]]
            yield sse_frame({"type": "citation", "items": items}, seq=seqs.take())
            trace.add_span_snapshot("citation", "retrieval", 0, count=len(items))
            # 游客不落库：无身份来源的引用记录没有审计价值，反而污染检索统计
            if identity.persist:
                for it in items:
                    enqueue(PersistItem(kind="citation", payload={
                        **it,
                        "trace_id": trace.trace_id,
                        "turn_id": turn_id,
                        "user_id": user_id,
                        "session_id": req.session_id,
                    }))

        await _persist_message(
            redis, identity, req.session_id, role="assistant",
            content=final_reply, turn_id=turn_id, persist=identity.persist,
            seq=seqs.take(),
        )
        if appointment_payload:
            await _persist_message(
                redis, identity, req.session_id, role="assistant",
                content="", event_type="appointment",
                appointment_data=appointment_payload, turn_id=turn_id,
                persist=identity.persist, seq=seqs.take(),
            )
        await _idem_store_result(redis, user_id, req.client_message_id, final_reply, appointment_payload)

        # ── 待确认动作：下发确认卡片（B2）───────────────────────────
        # 走到这里仍是 pending_confirm 的才是真正"在等患者点头"的动作；
        # 本轮已被确认/拒绝的，状态已离开 pending_confirm，这里不会再下发。
        if identity.persist:
            confirm_view = await _pending_confirm_payload(thread_id)
            if confirm_view:
                yield sse_frame({"type": "confirm_request", "data": confirm_view}, seq=seqs.take())
                trace.add_span_snapshot(
                    "confirm_request", "action", 0, action_id=confirm_view.get("id"),
                )

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
        # 清理段必须"抗取消"：客户端断开 / 总超时会 cancel 本任务，此后 finally 里
        # 任何一个 await 都会立刻抛 CancelledError（它继承 BaseException，
        # except Exception 拦不住），清理就此中断。三个后果都由实测/代码确认：
        #   1. 并发槽漏减 → 用户 300s 内一直被"您有问诊正在进行中"挡住；
        #   2. sse_limiter 连接计数漏减 → 该计数只减不增、无 TTL，最终所有新连接被拒；
        #   3. 同会话锁漏删 → 靠 px TTL 自愈，影响相对小。
        # shield 让被包裹的清理独立跑完，不受这次取消影响。
        if slot_acquired:
            await _run_cleanup(lambda: release_user_slot(redis, slot_key))
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        if lock_held:
            await _run_cleanup(lambda: redis.delete(f"lock:turn:{thread_id}"))
        if seqs is not None:
            await _run_cleanup(seqs.sync)
        reset_budget(budget_token)
        if trace is not None:
            if api_span is not None:
                trace.end_span(
                    api_span,
                    status="ok" if outcome in ("success", "blocked") else "error",
                    error_type=error_type,
                )
            end_trace(trace, trace_token, status=outcome, error_type=error_type)
        await _run_cleanup(sse_limiter.exit)


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