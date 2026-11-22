# src/api/routers/stats.py
#
# 统计与轨迹 API：
# - GET /api/v1/stats/tokens          ：token 用量聚合（group_by=day|model|worker）
# - GET /api/v1/traces                ：Agent 轨迹（按 turn_id 聚合 token_usage）
# - GET /api/v1/stats/consultations   ：问诊统计（按日/科室/紧急程度）
# - GET /api/v1/stats/symptoms        ：症状 TopN（从问诊主诉分词聚合，精度有限）

from __future__ import annotations

import asyncio
from collections import Counter
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import select, func, cast, Date
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import AdminIdentity, get_current_admin
from src.infra.database import AsyncSessionLocal, get_db
from src.modules.medical.model import Consultation, Department
from src.modules.ops.model import TokenUsage

# 统计/轨迹都是平台运营数据，只开放给管理端（管理员 token）
router = APIRouter(tags=["stats"], dependencies=[Depends(get_current_admin)])

TRACE_SCAN_LIMIT = 2000     # 轨迹查询最多扫描的 token_usage 行数
SYMPTOM_SCAN_LIMIT = 2000   # 症状 TopN 最多扫描的主诉条数


def _parse_moment(value: str | None):
    """把 'YYYY-MM-DD' 解析成 datetime，让时间条件能命中索引。

    旧写法是 created_at <= f"{end} 23:59:59"，字符串比较既走不了索引，
    也会漏掉 23:59:59 之后的记录。
    """
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return value  # 无法解析时原样交给数据库判断


@router.get("/api/v1/stats/tokens")
async def token_stats(
    group_by: str = "day",  # day | model | worker
    start: str | None = None,
    end: str | None = None,
    session_id: str | None = None,
    user_id: str | None = None,
    limit: int = 100,
    admin: AdminIdentity = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """
    token 用量聚合统计。
    - group_by=day    ：按天聚合（key 为 YYYY-MM-DD）
    - group_by=model  ：按模型聚合
    - group_by=worker ：按调用来源聚合（supervisor/inquiry/drug/...）

    聚合下推到 SQL：此前会把 2 万行拉回 Python 里循环求和，随数据量线性劣化。
    """
    if group_by == "model":
        group_col = TokenUsage.model
        key_expr = func.coalesce(group_col, "unknown").label("key")
    elif group_by == "worker":
        group_col = TokenUsage.worker
        key_expr = func.coalesce(group_col, "unknown").label("key")
    else:
        # 按天分组时，Date 类型不能和字符串 coalesce，直接用 date
        group_col = cast(TokenUsage.created_at, Date)
        key_expr = group_col.label("key")
    q = select(
        key_expr,
        func.count().label("calls"),
        func.coalesce(func.sum(TokenUsage.prompt_tokens), 0).label("prompt_tokens"),
        func.coalesce(func.sum(TokenUsage.completion_tokens), 0).label("completion_tokens"),
        func.coalesce(func.sum(TokenUsage.total_tokens), 0).label("total_tokens"),
        func.coalesce(func.sum(TokenUsage.latency_ms), 0).label("latency_ms"),
    ).where(TokenUsage.tenant_id == admin.tenant_id)

    start_at = _parse_moment(start)
    if start_at:
        q = q.where(TokenUsage.created_at >= start_at)
    end_at = _parse_moment(end)
    if end_at and isinstance(end_at, datetime):
        # 含当天全部记录：用次日零点做开区间，避免 23:59:59 的截断问题
        q = q.where(TokenUsage.created_at < end_at + timedelta(days=1))
    elif end_at:
        q = q.where(TokenUsage.created_at <= end_at)
    if session_id:
        q = q.where(TokenUsage.session_id == session_id)
    if user_id:
        q = q.where(TokenUsage.user_id == user_id)

    # 取最新的 limit 个分组，再还原成升序输出（与旧接口顺序一致）
    q = q.group_by(key_expr).order_by(key_expr.desc()).limit(limit)
    rows = (await db.execute(q)).all()

    items = []
    for r in reversed(rows):
        calls = r.calls or 0
        items.append({
            "key": str(r.key),
            "calls": calls,
            "prompt_tokens": int(r.prompt_tokens or 0),
            "completion_tokens": int(r.completion_tokens or 0),
            "total_tokens": int(r.total_tokens or 0),
            "latency_ms": int(r.latency_ms or 0),
            "avg_latency_ms": int((r.latency_ms or 0) / calls) if calls else 0,
        })
    return {"groups": items, "total": len(items)}


@router.get("/api/v1/traces")
async def list_traces(
    session_id: str | None = None,
    user_id: str | None = None,
    worker: str | None = None,
    limit: int = 50,
    offset: int = 0,
    admin: AdminIdentity = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """
    Agent 轨迹：按 turn_id 聚合 token_usage，输出每轮对话的调用时间线。
    轨迹 = 一次用户消息触发的多次 LLM 调用（supervisor → worker → ...）。
    """
    q = (
        select(TokenUsage)
        .where(TokenUsage.tenant_id == admin.tenant_id)
        .order_by(TokenUsage.created_at.desc(), TokenUsage.id.desc())
        .limit(TRACE_SCAN_LIMIT)
    )
    if session_id:
        q = q.where(TokenUsage.session_id == session_id)
    if user_id:
        q = q.where(TokenUsage.user_id == user_id)
    if worker:
        q = q.where(TokenUsage.worker == worker)

    rows = (await db.execute(q)).scalars().all()

    traces: dict[str, dict] = {}
    order: list[str] = []
    for r in rows:
        tid = r.turn_id or f"noturn-{r.id}"
        if tid not in traces:
            traces[tid] = {
                "turn_id": tid,
                "session_id": r.session_id,
                "user_id": r.user_id,
                "created_at": str(r.created_at) if r.created_at else None,
                "items": [],
                "total_tokens": 0,
            }
            order.append(tid)
        traces[tid]["items"].append({
            "worker": r.worker,
            "model": r.model,
            "prompt_tokens": r.prompt_tokens,
            "completion_tokens": r.completion_tokens,
            "total_tokens": r.total_tokens,
            "latency_ms": r.latency_ms,
        })
        traces[tid]["total_tokens"] += r.total_tokens or 0

    all_traces = [traces[tid] for tid in order]
    return {"traces": all_traces[offset:offset + limit], "total": len(all_traces)}


async def _consultation_breakdowns(since: datetime, tenant_id: int) -> tuple[list, list, list]:
    """按日 / 科室 / 紧急程度三条聚合并发执行。

    AsyncSession 不支持并发操作，必须一条查询一个 session，
    否则会触发 "concurrent operations are not permitted"。
    """
    async def _by_day():
        async with AsyncSessionLocal() as db:
            return (await db.execute(
                select(
                    cast(Consultation.created_at, Date).label("day"),
                    func.count().label("cnt"),
                )
                .where(
                    Consultation.created_at >= since,
                    Consultation.tenant_id == tenant_id,
                )
                .group_by("day")
                .order_by("day")
            )).all()

    async def _by_department():
        async with AsyncSessionLocal() as db:
            return (await db.execute(
                select(
                    func.coalesce(Department.name, "未知").label("name"),
                    func.count().label("cnt"),
                )
                .select_from(Consultation)
                .outerjoin(Department, Consultation.department_id == Department.id)
                .where(
                    Consultation.created_at >= since,
                    Consultation.tenant_id == tenant_id,
                )
                .group_by("name")
                .order_by(func.count().desc())
            )).all()

    async def _by_urgency():
        async with AsyncSessionLocal() as db:
            return (await db.execute(
                select(Consultation.urgency_level, func.count())
                .where(
                    Consultation.created_at >= since,
                    Consultation.tenant_id == tenant_id,
                )
                .group_by(Consultation.urgency_level)
            )).all()

    return await asyncio.gather(_by_day(), _by_department(), _by_urgency())


@router.get("/api/v1/stats/consultations")
async def consultation_stats(
    days: int = 30,
    admin: AdminIdentity = Depends(get_current_admin),
):
    """问诊统计：按日问诊量 / 科室分布 / 紧急程度分布。

    三条聚合各自开一个 session 并发执行，因此不需要再占用注入进来的会话。
    """
    since = datetime.now() - timedelta(days=days)
    day_rows, dept_rows, urgency_rows = await _consultation_breakdowns(since, admin.tenant_id)

    return {
        "days": days,
        "by_day": [{"date": str(r.day), "count": r.cnt} for r in day_rows],
        "by_department": [{"name": r.name, "count": r.cnt} for r in dept_rows],
        "by_urgency": [{"level": r[0] or "normal", "count": r[1]} for r in urgency_rows],
    }


@router.get("/api/v1/stats/symptoms")
async def symptom_stats(
    top: int = 10,
    admin: AdminIdentity = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """
    症状 TopN：从问诊主诉（chief_complaint）按顿号/逗号切分后计数。
    注意：简单文本切分，非严格医学实体识别，仅供看板参考。
    """
    rows = (await db.execute(
        select(Consultation.chief_complaint)
        .where(
            Consultation.chief_complaint.isnot(None),
            Consultation.tenant_id == admin.tenant_id,
        )
        .order_by(Consultation.created_at.desc())
        .limit(SYMPTOM_SCAN_LIMIT)
    )).scalars().all()

    counter: Counter[str] = Counter()
    for complaint in rows:
        for token in complaint.replace("，", "、").replace(",", "、").replace(";", "、").replace("；", "、").split("、"):
            token = token.strip()
            if token:
                counter[token] += 1

    items = [{"name": name, "count": cnt} for name, cnt in counter.most_common(top)]
    return {"items": items, "total": len(items)}
