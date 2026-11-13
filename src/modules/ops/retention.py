# src/core/retention.py
#
# 访问日志与模型调用记录的保留周期清理。
#
# - agent_spans / agent_traces：TRACE_RETENTION_DAYS（默认 180 天）
# - safety_events：SAFETY_EVENT_RETENTION_DAYS（默认 365 天），
#   但 review_status=pending 的事件不清理（人工复核未完成不能丢证据）
#
# 触发方式：管理端 POST /api/v1/observability/retention/purge，
# 生产建议由定时任务（cron / 调度平台）每日调用一次。

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import get_settings
from src.modules.ops.trace_model import AgentSpan, AgentTrace, SafetyEvent


async def purge_expired(db: AsyncSession) -> dict:
    """清理超过保留期的追踪与安全事件，返回各表删除条数。"""
    settings = get_settings()
    now = datetime.now(timezone.utc)
    trace_cutoff = now - timedelta(days=settings.TRACE_RETENTION_DAYS)
    safety_cutoff = now - timedelta(days=settings.SAFETY_EVENT_RETENTION_DAYS)

    span_result = await db.execute(
        delete(AgentSpan).where(AgentSpan.created_at < trace_cutoff)
    )
    trace_result = await db.execute(
        delete(AgentTrace).where(AgentTrace.created_at < trace_cutoff)
    )
    safety_result = await db.execute(
        delete(SafetyEvent).where(
            SafetyEvent.created_at < safety_cutoff,
            SafetyEvent.review_status != "pending",
        )
    )
    await db.commit()

    return {
        "trace_retention_days": settings.TRACE_RETENTION_DAYS,
        "safety_event_retention_days": settings.SAFETY_EVENT_RETENTION_DAYS,
        "access_log_retention_days": settings.ACCESS_LOG_RETENTION_DAYS,
        "deleted_spans": span_result.rowcount or 0,
        "deleted_traces": trace_result.rowcount or 0,
        "deleted_safety_events": safety_result.rowcount or 0,
    }


async def retention_stats(db: AsyncSession) -> dict:
    """保留期概况：各表最早记录时间与总量，便于确认清理是否生效。"""
    settings = get_settings()
    traces = (await db.execute(select(func.count()).select_from(AgentTrace))).scalar() or 0
    spans = (await db.execute(select(func.count()).select_from(AgentSpan))).scalar() or 0
    oldest = (await db.execute(select(func.min(AgentTrace.created_at)))).scalar()
    pending_reviews = (
        await db.execute(
            select(func.count()).select_from(SafetyEvent).where(SafetyEvent.review_status == "pending")
        )
    ).scalar() or 0
    return {
        "traces": traces,
        "spans": spans,
        "oldest_trace_at": str(oldest) if oldest else None,
        "pending_safety_reviews": pending_reviews,
        "trace_retention_days": settings.TRACE_RETENTION_DAYS,
        "safety_event_retention_days": settings.SAFETY_EVENT_RETENTION_DAYS,
        "access_log_retention_days": settings.ACCESS_LOG_RETENTION_DAYS,
    }
