# src/api/routers/handoff.py
#
# C3 人工接管接口（工单闭环 + LangGraph interrupt 挂起/恢复）。
#
# 三类调用方：
#   患者侧  —— /api/v1/handoff/tickets        申请转人工、看自己的单、撤回
#   坐席侧  —— /api/v1/handoff/queue 与 .../claim|reply|resolve
#   管理端  —— /api/v1/admin/handoff/*        只读列表 + 授/回收坐席角色
#
# 挂起/恢复在别的文件里：
#   模型侧转人工 → agents/supervisor_agent.py 装配的 HandoffInterruptMiddleware
#   结单/撤回续跑 → modules/handoff/resume.py 的 Command(resume=...)
#   这个路由只负责"结单之后把挂起的一轮放掉"这一步的编排。

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import (
    AdminIdentity,
    AuthContext,
    get_current_admin,
    require_auth,
    require_scope,
)
from src.core.exceptions import BizErrorCode, BizException
from src.infra.database import get_db
from src.modules.handoff import service as svc
from src.modules.handoff.model import PRIORITIES, REASONS

router = APIRouter(prefix="/api/v1/handoff", tags=["handoff"])


def _bad(message: str) -> BizException:
    return BizException(BizErrorCode.PARAM_ERROR, message, status_code=400)


def _nf(message: str) -> BizException:
    return BizException(BizErrorCode.NOT_FOUND, message, status_code=404)


def _conflict(message: str) -> BizException:
    return BizException(BizErrorCode.CONFLICT, message, status_code=409)


def _forbidden(message: str) -> BizException:
    return BizException(BizErrorCode.FORBIDDEN, message, status_code=403)


class TicketCreate(BaseModel):
    session_id: str = Field(min_length=1, max_length=100)
    reason: str = Field(default="patient_request", max_length=32)
    priority: str = Field(default="normal", max_length=16)
    summary: str = Field(default="", max_length=500)
    payload: dict | None = None


class TicketReply(BaseModel):
    content: str = Field(min_length=1, max_length=2000)


class TicketResolve(BaseModel):
    resolution: str = Field(default="", max_length=500)


async def _agent_name(db: AsyncSession, agent_id: int) -> str:
    """坐席显示名：优先真实姓名，取不到用账号 ID 兜底。"""
    from sqlalchemy import select

    from src.modules.medical.model import Patient

    name = (
        await db.execute(select(Patient.name).where(Patient.id == agent_id))
    ).scalar_one_or_none()
    return str(name or f"坐席#{agent_id}")


# ---------------------------------------------------------------- 患者侧


@router.post("/tickets")
async def create_ticket(
    req: TicketCreate,
    ctx: AuthContext = Depends(require_auth()),
    db: AsyncSession = Depends(get_db),
):
    """患者申请转人工。同会话已有未结束工单时直接返回它（created=false）。"""
    user_id = ctx.subject_id
    if not user_id.isdigit():
        raise _bad("只有已登录的患者账号能申请人工接管")
    if req.reason not in REASONS:
        raise _bad(f"reason 只能是 {REASONS}")
    if req.priority not in PRIORITIES:
        raise _bad(f"priority 只能是 {PRIORITIES}")

    ticket, created = await svc.create_ticket(
        db,
        patient_id=int(user_id),
        session_id=req.session_id,
        reason=req.reason,
        priority=req.priority,
        summary=req.summary,
        payload=req.payload,
        tenant_id=ctx.tenant_id,
    )
    return {"created": created, "ticket": svc.serialize(ticket)}


@router.get("/tickets/mine")
async def my_tickets(
    session_id: str | None = None,
    active_only: bool = True,
    ctx: AuthContext = Depends(require_auth()),
    db: AsyncSession = Depends(get_db),
):
    """患者看自己的工单（默认只看未结束的）。"""
    user_id = ctx.subject_id
    if not user_id.isdigit():
        raise _bad("只有已登录的患者账号能查看工单")
    rows = await svc.list_tickets(
        db,
        patient_id=int(user_id),
        only_active=bool(active_only),
        limit=20,
        tenant_id=ctx.tenant_id,
    )
    items = [svc.serialize(t) for t in rows]
    if session_id:
        items = [i for i in items if i["session_id"] == session_id]
    return {"items": items, "total": len(items)}


# ---------------------------------------------------------------- 实时推送（SSE）


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


@router.get("/stream")
async def handoff_stream(
    request: Request,
    scope: str = Query(default="patient", pattern="^(patient|seat)$"),
    ctx: AuthContext = Depends(require_auth()),
):
    """工单实时推送。

    - `scope=patient`（默认）：只推"我自己的工单"。先下发一帧快照，之后
      工单状态变化推 `ticket`、坐席/收尾消息推 `message`。
    - `scope=seat`：给坐席台推"队列变了"信号（`type=queue`），它自己重拉列表，
      避免把筛选/选中态塞进推送逻辑里。

    用 fetch + Bearer 头消费（不是 EventSource）：EventSource 不能带
    Authorization，只能把 token 塞进 URL，那会把凭证写进代理日志与浏览器历史。
    """
    from src.modules.handoff.bus import SEAT_TOPIC, get_handoff_bus, patient_topic

    bus = get_handoff_bus()

    if scope == "seat":
        if not ctx.has_scope("handoff:read"):
            raise _forbidden("当前账号没有工单查看权限")
        topic = SEAT_TOPIC
        initial: dict = {"type": "ready", "scope": "seat"}
    else:
        if not ctx.subject_id.isdigit():
            raise _forbidden("只有已登录的患者账号能订阅工单状态")
        topic = patient_topic(int(ctx.subject_id))
        # 首帧快照只查一次，且用完就还连接：SSE 会挂很久，
        # 不能用 Depends(get_db) 把连接池里的连接一直占到这个标签页关掉。
        from src.infra.database import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            rows = await svc.list_tickets(
                db, patient_id=int(ctx.subject_id), only_active=True, limit=10,
                tenant_id=ctx.tenant_id,
            )
            items = [svc.serialize(t) for t in rows]
        initial = {"type": "snapshot", "items": items}

    async def gen():
        queue = await bus.subscribe(topic)
        try:
            yield _sse(initial)
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield _sse(event)
        except asyncio.CancelledError:
            # 浏览器关标签页 → ASGI 取消流；吞掉，让 finally 正常退订
            raise
        finally:
            await bus.unsubscribe(topic, queue)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            # 关掉 nginx 缓冲，否则事件会被攒到连接关闭才一起吐出来
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/tickets/{ticket_id}/cancel")
async def cancel_ticket(
    ticket_id: int,
    ctx: AuthContext = Depends(require_auth()),
    db: AsyncSession = Depends(get_db),
):
    if not ctx.subject_id.isdigit():
        raise _bad("只有已登录的患者账号能撤回工单")
    try:
        ticket = await svc.cancel_ticket(
            db, ticket_id=ticket_id, patient_id=int(ctx.subject_id),
            tenant_id=ctx.tenant_id,
        )
    except LookupError:
        raise _nf("工单不存在")
    except PermissionError:
        raise _forbidden("只能撤回自己的工单")
    except RuntimeError as e:
        raise _conflict(str(e))

    # 撤回也要把挂起的一轮放掉，否则那张 thread 永远停在 interrupt 上，
    # 患者下次发消息还在等一个不会来的坐席。
    from src.modules.handoff import resume as handoff_resume

    closing = await handoff_resume.resume_handoff(ticket, cancelled=True) or \
        handoff_resume.fallback_closing(ticket, cancelled=True)
    await handoff_resume.persist_closing(ticket, closing)
    return {"ticket": svc.serialize(ticket), "closing": closing}


@router.get("/tickets/{ticket_id}")
async def ticket_detail(
    ticket_id: int,
    ctx: AuthContext = Depends(require_auth()),
    db: AsyncSession = Depends(get_db),
):
    """详情：本人可看，坐席（handoff:read）可看。

    C1：先按 tenant_id 卡一道 —— 只比 patient_id 时，另一租户里同 patient_id 的
    影子工单会被当成"我的单"返回 200，而不是 404。
    """
    ticket = await svc.get_ticket(db, ticket_id)
    if ticket is None or int(ticket.tenant_id) != int(ctx.tenant_id):
        raise _nf("工单不存在")
    is_owner = ctx.subject_id.isdigit() and int(ctx.subject_id) == int(ticket.patient_id)
    is_agent = ctx.has_scope("handoff:read")
    if not (is_owner or is_agent):
        raise _forbidden("无权查看该工单")
    return {"ticket": svc.serialize(ticket)}


# ---------------------------------------------------------------- 坐席侧


@router.get("/queue")
async def agent_queue(
    status: str = "",
    only_active: bool = True,
    mine: bool = False,
    limit: int = Query(default=30, ge=1, le=100),
    ctx: AuthContext = Depends(require_scope("handoff:read")),
    db: AsyncSession = Depends(get_db),
):
    """坐席台列表。mine=true 只看自己认领的。"""
    assigned_to = int(ctx.subject_id) if (mine and ctx.subject_id.isdigit()) else None
    rows = await svc.list_tickets(
        db,
        status=status,
        assigned_to=assigned_to,
        only_active=only_active,
        limit=limit,
        tenant_id=ctx.tenant_id,
    )
    return {
        "items": [svc.serialize(t) for t in rows],
        "total": len(rows),
        "stats": await svc.ticket_stats(db, tenant_id=ctx.tenant_id),
    }


@router.post("/tickets/{ticket_id}/claim")
async def claim(
    ticket_id: int,
    ctx: AuthContext = Depends(require_scope("handoff:write")),
    db: AsyncSession = Depends(get_db),
):
    if not ctx.subject_id.isdigit():
        raise _bad("当前身份不能认领工单")
    try:
        ticket = await svc.claim_ticket(
            db,
            ticket_id=ticket_id,
            agent_id=int(ctx.subject_id),
            agent_name=await _agent_name(db, int(ctx.subject_id)),
            tenant_id=ctx.tenant_id,
        )
    except LookupError:
        raise _nf("工单不存在")
    except RuntimeError as e:
        raise _conflict(str(e))
    return {"ticket": svc.serialize(ticket)}


@router.post("/tickets/{ticket_id}/reply")
async def reply(
    ticket_id: int,
    req: TicketReply,
    ctx: AuthContext = Depends(require_scope("handoff:write")),
    db: AsyncSession = Depends(get_db),
):
    """坐席回复：写回患者会话消息（chat_messages，event_type=human_agent）。"""
    if not ctx.subject_id.isdigit():
        raise _bad("当前身份不能回复工单")
    try:
        ticket = await svc.reply_ticket(
            db,
            ticket_id=ticket_id,
            agent_id=int(ctx.subject_id),
            content=req.content,
            tenant_id=ctx.tenant_id,
        )
    except LookupError:
        raise _nf("工单不存在")
    except RuntimeError as e:
        raise _conflict(str(e))
    except ValueError as e:
        raise _bad(str(e))
    logger.info(f"坐席回复工单 #{ticket_id} by {ctx.subject_id}: {req.content[:40]}")
    return {"ticket": svc.serialize(ticket)}


@router.post("/tickets/{ticket_id}/resolve")
async def resolve(
    ticket_id: int,
    req: TicketResolve,
    ctx: AuthContext = Depends(require_scope("handoff:write")),
    db: AsyncSession = Depends(get_db),
):
    if not ctx.subject_id.isdigit():
        raise _bad("当前身份不能关闭工单")
    try:
        ticket = await svc.resolve_ticket(
            db,
            ticket_id=ticket_id,
            agent_id=int(ctx.subject_id),
            resolution=req.resolution,
            tenant_id=ctx.tenant_id,
        )
    except LookupError:
        raise _nf("工单不存在")
    except RuntimeError as e:
        raise _conflict(str(e))

    # 结单后把被挂起的那一轮续跑：Command(resume=...) → 模型生成收尾话术写回会话。
    # 失败不影响工单状态（见 resume.py 的防御说明），这里把它降级成空串。
    from src.modules.handoff import resume as handoff_resume

    closing = await handoff_resume.resume_handoff(
        ticket,
        reply=ticket.resolution,
        resolution=ticket.resolution,
        agent_name=ticket.assigned_name,
    ) or handoff_resume.fallback_closing(ticket)
    await handoff_resume.persist_closing(ticket, closing)
    return {"ticket": svc.serialize(ticket), "closing": closing}


async def _messages_of_ticket(db: AsyncSession, ticket, limit: int) -> dict:
    """该工单对应会话的最近消息（坐席台与会端管理端共用同一套口径）。

    会话按 (user_id, session_id, tenant_id) 定位：只按前两者时，
    另一租户里同号主体的会话会被拿来当作本单的上下文。
    """
    from sqlalchemy import desc, select

    from src.modules.chat.model import ChatMessage, Conversation

    tenant_id = int(getattr(ticket, "tenant_id", 1) or 1)
    conv_id = ticket.conversation_id
    if conv_id is None:
        conv_id = (
            await db.execute(
                select(Conversation.id).where(
                    Conversation.user_id == str(ticket.patient_id),
                    Conversation.session_id == str(ticket.session_id),
                    Conversation.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
    if conv_id is None:
        return {"items": [], "total": 0}

    rows = (
        await db.execute(
            select(ChatMessage)
            .where(
                ChatMessage.conversation_id == int(conv_id),
                ChatMessage.tenant_id == tenant_id,
                ChatMessage.content != "",
            )
            .order_by(desc(ChatMessage.seq))
            .limit(limit)
        )
    ).scalars().all()
    items = [
        {
            "seq": int(m.seq),
            "role": str(m.role),
            "content": str(m.content_redacted or m.content or ""),
            "event_type": str(m.event_type or ""),
        }
        for m in reversed(rows)
    ]
    return {"items": items, "total": len(items)}


@router.get("/tickets/{ticket_id}/messages")
async def ticket_messages(
    ticket_id: int,
    limit: int = Query(default=20, ge=1, le=100),
    ctx: AuthContext = Depends(require_scope("handoff:read")),
    db: AsyncSession = Depends(get_db),
):
    """该工单对应会话的最近消息 —— 坐席接单前要先知道患者说了什么。"""
    ticket = await svc.get_ticket(db, ticket_id)
    if ticket is None or int(ticket.tenant_id or 1) != int(ctx.tenant_id):
        raise _nf("工单不存在")
    return await _messages_of_ticket(db, ticket, limit)


# ---------------------------------------------------------------- 管理端（只读）


admin_router = APIRouter(
    prefix="/api/v1/admin/handoff",
    tags=["admin-handoff"],
    dependencies=[Depends(get_current_admin)],
)


@admin_router.get("/tickets")
async def admin_tickets(
    status: str = "",
    only_active: bool = False,
    limit: int = Query(default=30, ge=1, le=100),
    admin: AdminIdentity = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    rows = await svc.list_tickets(
        db, status=status, only_active=only_active, limit=limit,
        tenant_id=admin.tenant_id,
    )
    return {
        "items": [svc.serialize(t) for t in rows],
        "total": len(rows),
        "stats": await svc.ticket_stats(db, tenant_id=admin.tenant_id),
    }


@admin_router.get("/tickets/{ticket_id}")
async def admin_ticket_detail(
    ticket_id: int,
    admin: AdminIdentity = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """管理端工单详情（只读，不含认领/回复）。"""
    ticket = await svc.get_ticket(db, ticket_id)
    if ticket is None or int(ticket.tenant_id or 1) != int(admin.tenant_id):
        raise _nf("工单不存在")
    return {"ticket": svc.serialize(ticket)}


@admin_router.get("/tickets/{ticket_id}/messages")
async def admin_ticket_messages(
    ticket_id: int,
    limit: int = Query(default=30, ge=1, le=100),
    admin: AdminIdentity = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """管理端只读工单会话：审计"坐席到底跟患者说了什么"。"""
    ticket = await svc.get_ticket(db, ticket_id)
    if ticket is None or int(ticket.tenant_id or 1) != int(admin.tenant_id):
        raise _nf("工单不存在")
    return await _messages_of_ticket(db, ticket, limit)


class AgentGrant(BaseModel):
    note: str = Field(default="", max_length=255)


@admin_router.post("/agents/{patient_id}")
async def grant_service_role(
    patient_id: int,
    req: AgentGrant,
    admin: AdminIdentity = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """给院内账号授予"坐席"角色（人工接管用）。

    坐席不需要档案与科室绑定：它评的是"能接工单"，不是"能看某个科室的告警"。
    但必须是可用账号，否则授了也接不到单。
    """
    from sqlalchemy import select

    from src.core.permissions import KIND_PATIENT, ROLE_SERVICE
    from src.modules.medical.model import Patient
    from src.modules.rbac import service as rbac

    p = (
        await db.execute(select(Patient).where(Patient.id == patient_id))
    ).scalar_one_or_none()
    if p is None:
        raise _nf(f"账号 {patient_id} 不存在")
    if not p.enabled:
        raise _conflict("该账号已停用，不能授予坐席角色")
    granted = await rbac.grant_role(
        db, KIND_PATIENT, patient_id, ROLE_SERVICE,
        granted_by=f"admin:{admin.username or admin.admin_id}",
        note=req.note or "管理端授予坐席",
    )
    logger.info(f"管理端授予坐席角色：account={patient_id} newly={granted}")
    return {
        "granted": granted,
        "roles": await rbac.roles_of(db, KIND_PATIENT, patient_id),
    }


@admin_router.delete("/agents/{patient_id}")
async def revoke_service_role(
    patient_id: int,
    admin: AdminIdentity = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """回收坐席角色。只回收 service，不动 clinician —— 医生兼坐席的账号不应被一键清空。"""
    from src.core.permissions import KIND_PATIENT, ROLE_SERVICE
    from src.modules.rbac import service as rbac

    revoked = await rbac.revoke_role(db, KIND_PATIENT, patient_id, ROLE_SERVICE)
    # 角色没了，仍挂在他名下的未结束工单要放开，否则坐席台会卡在"别人认领中"
    from sqlalchemy import update

    from src.modules.handoff.model import ACTIVE_STATUSES, STATUS_OPEN, HandoffTicket

    freed = await db.execute(
        update(HandoffTicket)
        .where(
            HandoffTicket.assigned_to == patient_id,
            HandoffTicket.status.in_(ACTIVE_STATUSES),
        )
        .values(status=STATUS_OPEN, assigned_to=None, assigned_name="", claimed_at=None)
    )
    await db.commit()
    logger.info(f"管理端回收坐席角色：account={patient_id} revoked={revoked} freed={freed.rowcount}")
    return {
        "revoked": revoked,
        "freed_tickets": int(freed.rowcount or 0),
        "roles": await rbac.roles_of(db, KIND_PATIENT, patient_id),
    }
