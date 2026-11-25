# src/modules/handoff/service.py
#
# C3 人工接管：工单服务层。
#
# 职责边界：
#   - 这里只负责"工单本身"的读写与状态流转（建单 / 认领 / 回复 / 解决 / 关闭）；
#   - 对话正文不在这里存 —— 患者说的话在 chat_messages，坐席回复也写 chat_messages
#     （见 reply() 里的 append_agent_message），工单只记次数与最后时间；
#   - 权限判定在 router（scope: handoff:read / handoff:write），这里不做授权。
#
# 三条硬规则（都是踩过坑的反面）：
#   1. 同一会话同时只允许一张未结束工单 —— 患者连点三次"转人工"不该让坐席台出现三张单；
#   2. 认领是原子的（status=open 且 assigned_to 为空才不会被人抢），抢单失败报 409；
#   3. 回复必须先有归属：不是自己的工单不能回，防止两个坐席给同一患者各说一套。

from __future__ import annotations

from datetime import datetime
from typing import Any

from loguru import logger
from sqlalchemy import desc, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.infra.event_bus import publish

from src.modules.handoff.model import (
    ACTIVE_STATUSES,
    PRIORITIES,
    REASONS,
    STATUS_CANCELLED,
    STATUS_CLAIMED,
    STATUS_CLOSED,
    STATUS_OPEN,
    STATUS_RESOLVED,
    HandoffTicket,
)


def _gen_ticket_no() -> str:
    """工单号：HO + 年月日 + 6 位序号。人看着顺眼，也方便电话里报。"""
    import random

    return f"HO{datetime.now().strftime('%Y%m%d')}{random.randint(100000, 999999)}"


async def broadcast_ticket(ticket: HandoffTicket, message: dict[str, Any] | None = None) -> None:
    """工单落库后广播一次（患者端状态条 / 坐席台队列）。

    时间点必须在 commit 之后：前端收到事件会立刻重查接口，
    若此时数据还没提交，它读到的是旧状态，然后要等到下一次轮询才纠正。
    底层是 Redis Pub/Sub，多 worker 下也能投递到别的进程里的 SSE 订阅者。
    """
    try:
        from src.modules.handoff.bus import publish_ticket_event

        await publish_ticket_event(serialize(ticket), message=message)
    except Exception as e:  # noqa: BLE001
        # 推送失败不影响业务：前端 8~10s 轮询是兜底
        logger.warning(f"工单广播失败 {getattr(ticket, 'ticket_no', '')}: {type(e).__name__}: {e}")


def serialize(ticket: HandoffTicket) -> dict[str, Any]:
    def _dt(v: datetime | None) -> str:
        return v.strftime("%Y-%m-%d %H:%M:%S") if v else ""

    return {
        "id": int(ticket.id),
        "ticket_no": str(ticket.ticket_no or ""),
        "patient_id": int(ticket.patient_id),
        "session_id": str(ticket.session_id or ""),
        "conversation_id": int(ticket.conversation_id) if ticket.conversation_id else None,
        "status": str(ticket.status),
        "priority": str(ticket.priority),
        "reason": str(ticket.reason),
        "summary": str(ticket.summary or ""),
        "payload": ticket.payload or {},
        "assigned_to": int(ticket.assigned_to) if ticket.assigned_to else None,
        "assigned_name": str(ticket.assigned_name or ""),
        "claimed_at": _dt(ticket.claimed_at),
        "resolved_at": _dt(ticket.resolved_at),
        "resolution": str(ticket.resolution or ""),
        "message_count": int(ticket.message_count or 0),
        "last_message_at": _dt(ticket.last_message_at),
        "created_at": _dt(ticket.created_at),
    }


async def _active_ticket(
    db: AsyncSession, *, patient_id: int, session_id: str, tenant_id: int,
) -> HandoffTicket | None:
    return (
        await db.execute(
            select(HandoffTicket).where(
                HandoffTicket.tenant_id == tenant_id,
                HandoffTicket.patient_id == patient_id,
                HandoffTicket.session_id == session_id,
                HandoffTicket.status.in_(ACTIVE_STATUSES),
            ).order_by(desc(HandoffTicket.id)).limit(1)
        )
    ).scalar_one_or_none()


async def active_ticket(
    db: AsyncSession, *, patient_id: int, session_id: str, tenant_id: int = 1,
) -> HandoffTicket | None:
    """对外只读版：会话当前是否有未结束工单（chat 路由用它决定 AI 要不要让位）。"""
    return await _active_ticket(
        db, patient_id=patient_id, session_id=session_id, tenant_id=tenant_id,
    )


async def _ticket_by_dedupe(
    db: AsyncSession, *, dedupe_key: str, tenant_id: int = 1,
) -> HandoffTicket | None:
    """按幂等键找单 —— **不看状态**。

    LangGraph 的 interrupt 在恢复时会把整个节点重跑一遍（LangGraph 的既定行为），
    所以"建单"这段代码在 resume 时还会再执行一次。那时工单可能已经被坐席结单，
    按 ACTIVE_STATUSES 找就会找不到 → 又新建一张重复单。
    幂等键必须能跨越终态命中同一条记录。
    """
    if not dedupe_key:
        return None
    return (
        await db.execute(
            select(HandoffTicket)
            .where(
                HandoffTicket.tenant_id == tenant_id,
                HandoffTicket.payload["dedupe_key"].astext == dedupe_key,
            )
            .order_by(desc(HandoffTicket.id))
            .limit(1)
        )
    ).scalar_one_or_none()


async def _conversation_id(
    db: AsyncSession, *, patient_id: int, session_id: str, tenant_id: int,
) -> int | None:
    from src.modules.chat.model import Conversation

    return (
        await db.execute(
            select(Conversation.id).where(
                Conversation.tenant_id == tenant_id,
                Conversation.user_id == str(patient_id),
                Conversation.session_id == session_id,
            )
        )
    ).scalar_one_or_none()


async def create_ticket(
    db: AsyncSession,
    *,
    patient_id: int,
    session_id: str,
    reason: str = "patient_request",
    priority: str = "normal",
    summary: str = "",
    payload: dict | None = None,
    dedupe_key: str = "",
    commit: bool = True,
    tenant_id: int = 1,
) -> tuple[HandoffTicket, bool]:
    """建单；同会话已有未结束工单时直接复用它（返回 created=False）。

    复用而不是新建：患者重复点"转人工"时，坐席台应只有一张单，
    否则患者会收到多路回复，且认领/关闭状态互相打架。

    dedupe_key：LangGraph interrupt 恢复时会重跑节点，同一个 tool_call 会第二次调用
    本函数；带上幂等键后第二次直接命中已建的单（哪怕已经被结单），不会重复建单。
    """
    if not session_id:
        raise ValueError("session_id 不能为空")
    reason = reason if reason in REASONS else "patient_request"
    priority = priority if priority in PRIORITIES else "normal"

    if dedupe_key:
        existing = await _ticket_by_dedupe(db, dedupe_key=dedupe_key, tenant_id=tenant_id)
        if existing is not None:
            return existing, False

    existing = await _active_ticket(
        db, patient_id=patient_id, session_id=session_id, tenant_id=tenant_id,
    )
    if existing is not None:
        return existing, False

    row = HandoffTicket(
        tenant_id=int(tenant_id),
        ticket_no=_gen_ticket_no(),
        patient_id=patient_id,
        session_id=session_id,
        conversation_id=await _conversation_id(
            db, patient_id=patient_id, session_id=session_id, tenant_id=tenant_id,
        ),
        status=STATUS_OPEN,
        priority=priority,
        reason=reason,
        summary=str(summary or "")[:500],
        # created_at 显式用本地时间：表默认值 func.now() 取的是数据库时区（容器 UTC），
        # 而 claimed_at/resolved_at 都是 Python 本地时间，两者混在一张表里，
        # 前端就会出现"12:45 建单、20:45 认领"这种 8 小时错位。
        created_at=datetime.now(),
        updated_at=datetime.now(),
        payload={**(payload or {}), **({"dedupe_key": dedupe_key} if dedupe_key else {})},
    )
    db.add(row)
    if commit:
        await db.commit()
        await db.refresh(row)
    logger.info(f"人工接管建单：{row.ticket_no} patient={patient_id} reason={reason}")
    if commit:  # 走到这里一定是新单；复用原单在上面已 return，不重复通知
        await broadcast_ticket(row)
        # 事件驱动：建单事实投到总线，下游（通知 / 统计 / SLA 计时）各自消费，
        # 不再把"建单之后要做的所有事"塞进这个函数里。dedupe_key 用单号，
        # 重跑节点或客户端重试都不会投出两条一样的工单事件。
        await publish(
            "handoff.ticket.created",
            {
                "ticket_no": row.ticket_no,
                "patient_id": row.patient_id,
                "session_id": row.session_id,
                "reason": row.reason,
                "priority": row.priority,
            },
            dedupe_key=str(row.ticket_no),
            tenant_id=int(tenant_id),
        )
    return row, True


async def list_tickets(
    db: AsyncSession,
    *,
    status: str = "",
    patient_id: int | None = None,
    assigned_to: int | None = None,
    only_active: bool = False,
    limit: int = 30,
    tenant_id: int | None = None,
) -> list[HandoffTicket]:
    stmt = select(HandoffTicket)
    # tenant_id=None 仅用于运维/迁移类工具；业务入口（患者、坐席、管理端）一律传租户
    if tenant_id is not None:
        stmt = stmt.where(HandoffTicket.tenant_id == tenant_id)
    if only_active:
        stmt = stmt.where(HandoffTicket.status.in_(ACTIVE_STATUSES))
    elif status:
        stmt = stmt.where(HandoffTicket.status == status)
    if patient_id is not None:
        stmt = stmt.where(HandoffTicket.patient_id == patient_id)
    if assigned_to is not None:
        stmt = stmt.where(HandoffTicket.assigned_to == assigned_to)
    # 排队顺序：urgent 先、其次老单先到先服务
    stmt = stmt.order_by(
        desc(func.coalesce(HandoffTicket.priority, "normal")),
        HandoffTicket.id.asc(),
    ).limit(max(1, min(limit, 100)))
    return list((await db.execute(stmt)).scalars().all())


async def get_ticket(db: AsyncSession, ticket_id: int) -> HandoffTicket | None:
    return (
        await db.execute(select(HandoffTicket).where(HandoffTicket.id == ticket_id))
    ).scalar_one_or_none()


async def _ticket_in_tenant(
    db: AsyncSession, *, ticket_id: int, tenant_id: int | None,
) -> HandoffTicket:
    """按 (id, tenant_id) 取工单；租户不符按 404 处理，隐藏跨租户主键存在性。"""
    row = await get_ticket(db, ticket_id)
    if row is None:
        raise LookupError("工单不存在")
    if tenant_id is not None and int(row.tenant_id or 1) != int(tenant_id):
        raise LookupError("工单不存在")
    return row


async def claim_ticket(
    db: AsyncSession, *, ticket_id: int, agent_id: int, agent_name: str,
    tenant_id: int | None = None,
) -> HandoffTicket:
    """原子认领：只有 open 且无人认领的单能被抢到。"""
    row = await _ticket_in_tenant(db, ticket_id=ticket_id, tenant_id=tenant_id)
    if row.status != STATUS_OPEN or row.assigned_to is not None:
        raise RuntimeError(f"工单已被 {row.assigned_name or row.assigned_to} 认领或已结束")

    result = await db.execute(
        update(HandoffTicket)
        .where(
            HandoffTicket.id == ticket_id,
            HandoffTicket.status == STATUS_OPEN,
            HandoffTicket.assigned_to.is_(None),
        )
        .values(
            status=STATUS_CLAIMED,
            assigned_to=agent_id,
            assigned_name=str(agent_name or "")[:64],
            claimed_at=datetime.now(),
        )
    )
    if int(result.rowcount or 0) == 0:
        await db.rollback()
        raise RuntimeError("工单刚被其他人认领，请刷新列表")
    await db.commit()
    row = await get_ticket(db, ticket_id)
    logger.info(f"人工接管认领：{row.ticket_no if row else ticket_id} agent={agent_id}")
    if row is not None:
        await broadcast_ticket(row)
    return row


async def append_session_message(
    db: AsyncSession,
    *,
    ticket: HandoffTicket,
    content: str,
    role: str = "assistant",
    event_type: str = "human_agent",
) -> int | None:
    """往工单对应会话里追加一条消息（患者端读的就是这张表）。

    为什么不只存在工单里：患者端读的是会话消息，工单是坐席的工作台。
    只写工单等于"坐席说了但患者永远看不到"。
    """
    from src.modules.chat.model import ChatMessage, Conversation

    text = str(content or "").strip()
    if not text:
        return None

    conv_id = int(ticket.conversation_id) if ticket.conversation_id else None
    if conv_id is None:
        conv_id = await _conversation_id(
            db, patient_id=int(ticket.patient_id), session_id=str(ticket.session_id),
            tenant_id=int(getattr(ticket, "tenant_id", 1) or 1),
        )
        if conv_id is None:
            # 会话还没落库：建一条，保证这条回复有地方落
            conv = Conversation(
                tenant_id=int(getattr(ticket, "tenant_id", 1) or 1),
                user_id=str(ticket.patient_id),
                session_id=str(ticket.session_id),
                title="人工接管",
                last_seq=0,
            )
            db.add(conv)
            await db.flush()
            conv_id = int(conv.id)
            ticket.conversation_id = conv_id

    seq_row = (
        await db.execute(select(Conversation.last_seq).where(Conversation.id == conv_id))
    ).scalar_one_or_none()
    # seq 必须和聊天侧共用同一个计数器：聊天消息是批量异步落库的，
    # DB 的 last_seq 常常落后于已分配出去的号，若这里用 last_seq+1，
    # 就会和队列里还没落库的患者消息撞号，被 on_conflict_do_nothing 静默丢一条。
    try:
        from src.modules.chat.seq_allocator import next_seq
        from src.infra.redis_cache import get_redis_client

        seq = await next_seq(await get_redis_client(), str(ticket.patient_id), str(ticket.session_id))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"坐席消息 seq 分配降级为 DB 自增: {type(e).__name__}: {e}")
        seq = int(seq_row or 0) + 1

    await db.execute(
        pg_insert(ChatMessage).values(
            tenant_id=int(getattr(ticket, "tenant_id", 1) or 1),
            conversation_id=conv_id,
            seq=seq,
            turn_id=f"handoff:{int(ticket.id)}",
            role=role,
            content=text,
            content_redacted=text,
            event_type=event_type,
            appointment_data={"ticket_no": str(ticket.ticket_no or ""), "agent": str(ticket.assigned_name or "")},
        ).on_conflict_do_nothing(index_elements=["conversation_id", "seq"])
    )
    await db.execute(
        update(Conversation)
        .where(Conversation.id == conv_id)
        .values(last_seq=func.greatest(Conversation.last_seq, seq), last_active_at=datetime.now())
    )
    return seq


async def append_agent_message(
    db: AsyncSession,
    *,
    ticket: HandoffTicket,
    content: str,
) -> None:
    """坐席回复：写会话 + 刷新工单计数。"""
    seq = await append_session_message(db, ticket=ticket, content=content)
    if seq is None:
        return
    ticket.message_count = int(ticket.message_count or 0) + 1
    ticket.last_message_at = datetime.now()


async def reply_ticket(
    db: AsyncSession, *, ticket_id: int, agent_id: int, content: str,
    tenant_id: int | None = None,
) -> HandoffTicket:
    """坐席回复：必须已认领且认领人是自己。"""
    row = await _ticket_in_tenant(db, ticket_id=ticket_id, tenant_id=tenant_id)
    if row.status != STATUS_CLAIMED:
        raise RuntimeError("请先认领工单再回复")
    if int(row.assigned_to or 0) != int(agent_id):
        raise RuntimeError("该工单由其他坐席认领，不能代其回复")
    if not str(content or "").strip():
        raise ValueError("回复内容不能为空")

    await append_agent_message(db, ticket=row, content=str(content))
    await db.commit()
    await db.refresh(row)
    # 患者端要"坐席这句话立刻出现在会话里"，所以带上正文一起推
    await broadcast_ticket(
        row,
        message={
            "ticket_no": str(row.ticket_no or ""),
            "content": str(content),
            "event_type": "human_agent",
            "message_count": int(row.message_count or 0),
        },
    )
    # 坐席回复同样发事件：下游可做首响时长统计 / 质检抽样 / 消息镜像
    await publish(
        "handoff.ticket.replied",
        {
            "ticket_no": row.ticket_no,
            "patient_id": row.patient_id,
            "session_id": row.session_id,
            "message_count": int(row.message_count or 0),
        },
        dedupe_key=f"{row.ticket_no}:msg:{int(row.message_count or 0)}",
        tenant_id=int(row.tenant_id or 1),
    )
    return row


async def resolve_ticket(
    db: AsyncSession, *, ticket_id: int, agent_id: int, resolution: str = "",
    tenant_id: int | None = None,
) -> HandoffTicket:
    """坐席关闭工单（已处理完）。只有认领人能关。"""
    row = await _ticket_in_tenant(db, ticket_id=ticket_id, tenant_id=tenant_id)
    if row.status not in (STATUS_CLAIMED, STATUS_OPEN):
        raise RuntimeError(f"工单状态 {row.status} 不能关闭")
    if row.status == STATUS_CLAIMED and int(row.assigned_to or 0) != int(agent_id):
        raise RuntimeError("该工单由其他坐席认领，不能代其关闭")

    row.status = STATUS_RESOLVED
    row.assigned_to = row.assigned_to or agent_id
    row.resolved_at = datetime.now()
    row.resolution = str(resolution or "")[:500]
    # 处理说明是坐席侧的内部记录（审计/交接用），不是给患者的话术。
    # 早先版本把它当 human_agent 消息 append 进会话，患者会看到
    # "已回复用药叠加风险，保持原方案" 这种内部口吻；现在只落字段，
    # 患者侧只收 resume 产生的收尾文案（handoff_close）。
    await db.commit()
    await db.refresh(row)
    await broadcast_ticket(row)
    # 结单事件：SLA 统计（建单→结单时长）、满意度回访任务的触发点
    await publish(
        "handoff.ticket.resolved",
        {
            "ticket_no": row.ticket_no,
            "patient_id": row.patient_id,
            "session_id": row.session_id,
            "assigned_to": row.assigned_to,
        },
        dedupe_key=f"{row.ticket_no}:resolved",
        tenant_id=int(row.tenant_id or 1),
    )
    return row


async def cancel_ticket(
    db: AsyncSession, *, ticket_id: int, patient_id: int,
    tenant_id: int | None = None,
) -> HandoffTicket:
    """患者撤回：只有本人、且**还没人接**的单能撤（坐席已认领后撤回会给坐席造成白干）。"""
    row = await _ticket_in_tenant(db, ticket_id=ticket_id, tenant_id=tenant_id)
    if int(row.patient_id) != int(patient_id):
        raise PermissionError("只能撤回自己的工单")
    if row.status != STATUS_OPEN:
        raise RuntimeError(f"工单状态 {row.status} 不能撤回，请联系坐席处理")
    row.status = STATUS_CANCELLED
    row.resolved_at = datetime.now()
    await db.commit()
    await db.refresh(row)
    await broadcast_ticket(row)
    return row


async def ticket_stats(db: AsyncSession, *, tenant_id: int | None = None) -> dict[str, int]:
    """坐席台顶部计数：待认领 / 处理中 / 今日完成。tenant_id=None 表示不限租户。"""
    stmt = select(HandoffTicket.status, func.count())
    if tenant_id is not None:
        stmt = stmt.where(HandoffTicket.tenant_id == tenant_id)
    rows = (await db.execute(stmt.group_by(HandoffTicket.status))).all()
    out = {str(s): int(c) for s, c in rows}
    return {
        "open": out.get(STATUS_OPEN, 0),
        "claimed": out.get(STATUS_CLAIMED, 0),
        "resolved": out.get(STATUS_RESOLVED, 0),
        "closed": out.get(STATUS_CLOSED, 0),
        "cancelled": out.get(STATUS_CANCELLED, 0),
    }

