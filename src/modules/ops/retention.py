# src/modules/ops/retention.py
#
# 安全事件保留周期清理。
#
# 链路轨迹（agent_traces / agent_spans）已不再落库，统一由 Langfuse 管理
# （自托管实例自带 ClickHouse 的 TTL 配置），这里只管 safety_events：
#   SAFETY_EVENT_RETENTION_DAYS（默认 365 天），review_status=pending 的不清理
#   （人工复核未完成不能丢证据）。
#
# 触发方式：管理端 POST /api/v1/observability/retention/purge，
# 生产建议由定时任务（cron / 调度平台）每日调用一次。

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import get_settings
from src.modules.ops.safety_model import SafetyEvent


async def purge_expired(db: AsyncSession) -> dict:
    """清理超过保留期的安全事件，返回删除条数。"""
    settings = get_settings()
    now = datetime.now(timezone.utc)
    safety_cutoff = now - timedelta(days=settings.SAFETY_EVENT_RETENTION_DAYS)

    safety_result = await db.execute(
        delete(SafetyEvent).where(
            SafetyEvent.created_at < safety_cutoff,
            SafetyEvent.review_status != "pending",
        )
    )
    await db.commit()

    return {
        "trace_store": "langfuse",
        "safety_event_retention_days": settings.SAFETY_EVENT_RETENTION_DAYS,
        "access_log_retention_days": settings.ACCESS_LOG_RETENTION_DAYS,
        "deleted_safety_events": safety_result.rowcount or 0,
    }


async def retention_stats(db: AsyncSession) -> dict:
    """保留期概况：安全事件最早记录时间与总量，便于确认清理是否生效。"""
    settings = get_settings()
    total = (await db.execute(select(func.count()).select_from(SafetyEvent))).scalar() or 0
    oldest = (await db.execute(select(func.min(SafetyEvent.created_at)))).scalar()
    pending_reviews = (
        await db.execute(
            select(func.count()).select_from(SafetyEvent).where(SafetyEvent.review_status == "pending")
        )
    ).scalar() or 0
    return {
        "trace_store": "langfuse",
        "safety_events": total,
        "oldest_safety_event_at": str(oldest) if oldest else None,
        "pending_safety_reviews": pending_reviews,
        "safety_event_retention_days": settings.SAFETY_EVENT_RETENTION_DAYS,
        "access_log_retention_days": settings.ACCESS_LOG_RETENTION_DAYS,
    }
