# src/api/routers/observability.py
#
# 全链路追踪查询（仅管理端）。
#
# 访问控制：整路由 admin 守卫；普通患者 token 一律 403。
# 数据脱敏：agent_spans.attributes 与 safety_events.snippet 在写入前已脱敏，
# 这里不再返回原始消息内容（trace 表本身不存原文）。

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import AdminIdentity, get_current_admin
from src.modules.ops.retention import purge_expired, retention_stats
from src.infra.database import get_db
from src.modules.ops.trace_model import AgentSpan, AgentTrace, SafetyEvent

router = APIRouter(
    prefix="/api/v1/observability",
    tags=["observability"],
    dependencies=[Depends(get_current_admin)],
)


def _serialize_trace(t: AgentTrace) -> dict:
    return {
        "trace_id": t.trace_id,
        "request_id": t.request_id,
        "user_id": t.user_id,
        "session_id": t.session_id,
        "turn_id": t.turn_id,
        "entrypoint": t.entrypoint,
        "agent_name": t.agent_name,
        "agent_version": t.agent_version,
        "prompt_version": t.prompt_version,
        "model_name": t.model_name,
        "model_version": t.model_version,
        "status": t.status,
        "error_type": t.error_type,
        "latency_ms": t.latency_ms,
        "tokens": {
            "prompt": t.prompt_tokens,
            "completion": t.completion_tokens,
            "total": t.total_tokens,
        },
        "tool_calls": t.tool_calls,
        "tool_names": t.tool_names or [],
        "agent_calls": t.agent_calls,
        "retrieval": {
            "count": t.retrieval_count,
            "sources": t.retrieval_sources or [],
        },
        "safety": {
            "level": t.safety_level,
            "result": t.safety_result,
            "rules": t.safety_rules or [],
        },
        "input_chars": t.input_chars,
        "output_chars": t.output_chars,
        "created_at": t.created_at.isoformat() if t.created_at else None,
    }


def _serialize_span(s: AgentSpan) -> dict:
    return {
        "span_id": s.span_id,
        "parent_span_id": s.parent_span_id,
        "name": s.name,
        "kind": s.kind,
        "agent_name": s.agent_name,
        "agent_version": s.agent_version,
        "tool_name": s.tool_name,
        "model_name": s.model_name,
        "model_version": s.model_version,
        "status": s.status,
        "error_type": s.error_type,
        "latency_ms": s.latency_ms,
        "tokens": {
            "prompt": s.prompt_tokens,
            "completion": s.completion_tokens,
            "total": s.total_tokens,
        },
        "attributes": s.attributes or {},
        "created_at": s.created_at.isoformat() if s.created_at else None,
    }


@router.get("/traces")
async def list_traces(
    trace_id: str | None = None,
    user_id: str | None = None,
    session_id: str | None = None,
    agent_name: str | None = None,
    status: str | None = None,
    safety_level: str | None = None,
    days: int = 7,
    limit: int = 50,
    offset: int = 0,
    admin: AdminIdentity = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    since = datetime.utcnow() - timedelta(days=max(1, min(days, 90)))
    q = (
        select(AgentTrace)
        .where(AgentTrace.created_at >= since, AgentTrace.tenant_id == admin.tenant_id)
        .order_by(AgentTrace.created_at.desc())
        .limit(max(1, min(limit, 200)))
        .offset(max(0, offset))
    )
    for column, value in (
        (AgentTrace.trace_id, trace_id),
        (AgentTrace.user_id, user_id),
        (AgentTrace.session_id, session_id),
        (AgentTrace.agent_name, agent_name),
        (AgentTrace.status, status),
        (AgentTrace.safety_level, safety_level),
    ):
        if value:
            q = q.where(column == value)
    rows = (await db.execute(q)).scalars().all()
    return {"traces": [_serialize_trace(t) for t in rows], "total": len(rows), "days": days}


@router.get("/traces/{trace_id}")
async def get_trace(
    trace_id: str,
    admin: AdminIdentity = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    trace = (
        await db.execute(
            select(AgentTrace).where(
                AgentTrace.trace_id == trace_id,
                AgentTrace.tenant_id == admin.tenant_id,
            )
        )
    ).scalar_one_or_none()
    if trace is None:
        return {"trace": None, "spans": [], "events": []}
    spans = (
        await db.execute(
            select(AgentSpan)
            .where(AgentSpan.trace_id == trace_id, AgentSpan.tenant_id == admin.tenant_id)
            .order_by(AgentSpan.id.asc())
        )
    ).scalars().all()
    events = (
        await db.execute(
            select(SafetyEvent)
            .where(
                SafetyEvent.trace_id == trace_id,
                SafetyEvent.tenant_id == admin.tenant_id,
            )
            .order_by(SafetyEvent.id.asc())
        )
    ).scalars().all()
    return {
        "trace": _serialize_trace(trace),
        "spans": [_serialize_span(s) for s in spans],
        "events": [
            {
                "id": e.id,
                "stage": e.stage,
                "level": e.level,
                "action": e.action,
                "rule_id": e.rule_id,
                "review_status": e.review_status,
                "snippet": e.snippet,
                "created_at": e.created_at.isoformat() if e.created_at else None,
            }
            for e in events
        ],
    }


@router.get("/stats")
async def trace_stats(
    days: int = 7,
    admin: AdminIdentity = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    since = datetime.utcnow() - timedelta(days=max(1, min(days, 90)))
    tenant = AgentTrace.tenant_id == admin.tenant_id
    total = (
        await db.execute(
            select(func.count()).select_from(AgentTrace)
            .where(AgentTrace.created_at >= since, tenant)
        )
    ).scalar() or 0
    errors = (
        await db.execute(
            select(func.count())
            .select_from(AgentTrace)
            .where(
                AgentTrace.created_at >= since,
                AgentTrace.status.in_(("error", "timeout")),
                tenant,
            )
        )
    ).scalar() or 0
    blocked = (
        await db.execute(
            select(func.count())
            .select_from(AgentTrace)
            .where(
                AgentTrace.created_at >= since,
                AgentTrace.status == "blocked",
                tenant,
            )
        )
    ).scalar() or 0
    agg = (
        await db.execute(
            select(
                func.avg(AgentTrace.latency_ms),
                func.sum(AgentTrace.total_tokens),
                func.avg(AgentTrace.tool_calls),
                func.avg(AgentTrace.retrieval_count),
            ).where(AgentTrace.created_at >= since, tenant)
        )
    ).one()

    by_agent = (
        await db.execute(
            select(
                AgentTrace.agent_name,
                func.count(),
                func.avg(AgentTrace.latency_ms),
                func.sum(AgentTrace.total_tokens),
            )
            .where(AgentTrace.created_at >= since, tenant)
            .group_by(AgentTrace.agent_name)
        )
    ).all()
    by_model = (
        await db.execute(
            select(AgentTrace.model_name, func.count(), func.sum(AgentTrace.total_tokens))
            .where(AgentTrace.created_at >= since, AgentTrace.model_name != "", tenant)
            .group_by(AgentTrace.model_name)
        )
    ).all()
    safety = (
        await db.execute(
            select(AgentTrace.safety_level, func.count())
            .where(AgentTrace.created_at >= since, tenant)
            .group_by(AgentTrace.safety_level)
        )
    ).all()

    return {
        "days": days,
        "total": total,
        "errors": errors,
        "blocked": blocked,
        "error_rate": round(errors / total, 4) if total else 0,
        "avg_latency_ms": int(agg[0] or 0),
        "total_tokens": int(agg[1] or 0),
        "avg_tool_calls": round(float(agg[2] or 0), 2),
        "avg_retrieval": round(float(agg[3] or 0), 2),
        "by_agent": [
            {"agent": a or "unknown", "count": c, "avg_latency_ms": int(l or 0), "tokens": int(t or 0)}
            for a, c, l, t in by_agent
        ],
        "by_model": [
            {"model": m, "count": c, "tokens": int(t or 0)} for m, c, t in by_model
        ],
        "safety_distribution": [{"level": lvl or "info", "count": c} for lvl, c in safety],
    }


@router.get("/retention")
async def get_retention(db: AsyncSession = Depends(get_db)):
    return await retention_stats(db)


@router.post("/retention/purge")
async def run_retention_purge(db: AsyncSession = Depends(get_db)):
    """按保留周期清理过期轨迹与安全事件（可挂定时任务每日执行）。"""
    return await purge_expired(db)
