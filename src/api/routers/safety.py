# src/api/routers/safety.py
#
# Safety Engine 管理端：安全事件查询、人工复核、规则库查看、命中统计。
#
# 访问控制：整路由 admin 守卫；事件片段写入时已脱敏，查询不再二次暴露原文。

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import AdminIdentity, get_current_admin
from src.core.config import get_settings
from src.core.exceptions import BizErrorCode, BizException
from src.core.safety import safety_engine
from src.infra.database import get_db
from src.modules.ops.trace_model import SafetyEvent

router = APIRouter(
    prefix="/api/v1/safety",
    tags=["safety"],
    dependencies=[Depends(get_current_admin)],
)


class ReviewIn(BaseModel):
    decision: Literal["approved", "rejected"]
    note: str | None = Field(None, max_length=300)


def _serialize(e: SafetyEvent) -> dict:
    return {
        "id": e.id,
        "trace_id": e.trace_id,
        "request_id": e.request_id,
        "user_id": e.user_id,
        "session_id": e.session_id,
        "stage": e.stage,
        "level": e.level,
        "action": e.action,
        "rule_id": e.rule_id,
        "rule_name": e.rule_name,
        "category": e.category,
        "snippet": e.snippet,
        "requires_review": e.requires_review,
        "review_status": e.review_status,
        "reviewer_id": e.reviewer_id,
        "review_note": e.review_note,
        "reviewed_at": e.reviewed_at.isoformat() if e.reviewed_at else None,
        "created_at": e.created_at.isoformat() if e.created_at else None,
    }


@router.get("/events")
async def list_events(
    level: str | None = None,
    stage: str | None = None,
    action: str | None = None,
    review_status: str | None = None,
    user_id: str | None = None,
    days: int = 7,
    limit: int = 50,
    offset: int = 0,
    admin: AdminIdentity = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    since = datetime.utcnow() - timedelta(days=max(1, min(days, 365)))
    q = (
        select(SafetyEvent)
        .where(SafetyEvent.created_at >= since, SafetyEvent.tenant_id == admin.tenant_id)
        .order_by(SafetyEvent.created_at.desc())
        .limit(max(1, min(limit, 200)))
        .offset(max(0, offset))
    )
    for column, value in (
        (SafetyEvent.level, level),
        (SafetyEvent.stage, stage),
        (SafetyEvent.action, action),
        (SafetyEvent.review_status, review_status),
        (SafetyEvent.user_id, user_id),
    ):
        if value:
            q = q.where(column == value)
    rows = (await db.execute(q)).scalars().all()
    return {"events": [_serialize(e) for e in rows], "total": len(rows), "days": days}


@router.get("/events/pending")
async def list_pending(
    admin: AdminIdentity = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
    limit: int = 50,
):
    rows = (
        await db.execute(
            select(SafetyEvent)
            .where(
                SafetyEvent.review_status == "pending",
                SafetyEvent.tenant_id == admin.tenant_id,
            )
            .order_by(SafetyEvent.created_at.asc())
            .limit(max(1, min(limit, 200)))
        )
    ).scalars().all()
    return {"events": [_serialize(e) for e in rows], "total": len(rows)}


@router.post("/events/{event_id}/review")
async def review_event(
    event_id: int,
    payload: ReviewIn,
    admin: AdminIdentity = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """人工复核：确认拦截合理（approved）或误报（rejected）。"""
    event = (
        await db.execute(
            select(SafetyEvent).where(
                SafetyEvent.id == event_id,
                SafetyEvent.tenant_id == admin.tenant_id,
            )
        )
    ).scalar_one_or_none()
    if event is None:
        raise BizException(BizErrorCode.NOT_FOUND, "安全事件不存在", status_code=404)

    event.review_status = payload.decision
    event.reviewer_id = admin.admin_id
    event.review_note = payload.note
    event.reviewed_at = datetime.now(timezone.utc)
    await db.commit()
    return {"message": "复核结果已保存", "event": _serialize(event)}


@router.get("/stats")
async def safety_stats(
    days: int = 30,
    admin: AdminIdentity = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    since = datetime.utcnow() - timedelta(days=max(1, min(days, 365)))
    tenant = SafetyEvent.tenant_id == admin.tenant_id
    total = (
        await db.execute(
            select(func.count()).select_from(SafetyEvent)
            .where(SafetyEvent.created_at >= since, tenant)
        )
    ).scalar() or 0
    pending = (
        await db.execute(
            select(func.count())
            .select_from(SafetyEvent)
            .where(
                SafetyEvent.created_at >= since,
                SafetyEvent.review_status == "pending",
                tenant,
            )
        )
    ).scalar() or 0
    by_level = (
        await db.execute(
            select(SafetyEvent.level, func.count())
            .where(SafetyEvent.created_at >= since, tenant)
            .group_by(SafetyEvent.level)
        )
    ).all()
    by_category = (
        await db.execute(
            select(SafetyEvent.category, func.count())
            .where(SafetyEvent.created_at >= since, tenant)
            .group_by(SafetyEvent.category)
        )
    ).all()
    by_action = (
        await db.execute(
            select(SafetyEvent.action, func.count())
            .where(SafetyEvent.created_at >= since, tenant)
            .group_by(SafetyEvent.action)
        )
    ).all()
    top_rules = (
        await db.execute(
            select(SafetyEvent.rule_id, SafetyEvent.rule_name, func.count())
            .where(SafetyEvent.created_at >= since, SafetyEvent.rule_id != "", tenant)
            .group_by(SafetyEvent.rule_id, SafetyEvent.rule_name)
            .order_by(func.count().desc())
            .limit(10)
        )
    ).all()

    return {
        "days": days,
        "total": total,
        "pending_review": pending,
        "by_level": [{"level": lvl, "count": c} for lvl, c in by_level],
        "by_category": [{"category": cat or "general", "count": c} for cat, c in by_category],
        "by_action": [{"action": a, "count": c} for a, c in by_action],
        "top_rules": [
            {"rule_id": rid, "rule_name": rname, "count": c} for rid, rname, c in top_rules
        ],
    }


@router.get("/rules")
async def list_rules():
    """危险信号规则库（运营可据此评审词表与阈值）。"""
    return {"rules": safety_engine.list_rules(), "total": len(safety_engine.list_rules())}


@router.get("/limits")
async def list_limits():
    """当前生效的执行限额，便于前端/运营核对。"""
    s = get_settings()
    return {
        "agent_max_calls": s.AGENT_MAX_CALLS,
        "tool_max_calls": s.TOOL_MAX_CALLS,
        "turn_max_tokens": s.TURN_MAX_TOKENS,
        "turn_timeout_seconds": s.TURN_TIMEOUT,
        "retrieval_max_results": s.RETRIEVAL_MAX_RESULTS,
        "user_max_concurrent_tasks": s.USER_MAX_CONCURRENT_TASKS,
        "safety_enabled": s.SAFETY_ENABLED,
        "trace_retention_days": s.TRACE_RETENTION_DAYS,
        "safety_event_retention_days": s.SAFETY_EVENT_RETENTION_DAYS,
        "access_log_retention_days": s.ACCESS_LOG_RETENTION_DAYS,
    }
