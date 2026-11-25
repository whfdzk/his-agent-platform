# src/modules/clinical/events.py
#
# 临床事件落库服务（B3）。
#
# 与 actions.py 的分工：
#   actions.py 管"要执行的高危动作"（挂号/开单），有状态机、需要人确认；
#   events.py 管"已经发生的临床判定"（急症分级），只追加、不改写、可查询统计。
#
# 落库策略：green（未见红旗症状）不落 —— 那等于"没有风险"，每会话一条是纯噪声；
# red/orange/yellow 落库，用于医护看板、质控抽查与分级阈值调优。

from __future__ import annotations

from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.clinical.model import (
    EVENT_EMERGENCY,
    LEVEL_GREEN,
    LEVEL_ORANGE,
    LEVEL_RED,
    LEVEL_SEVERITY,
    LEVEL_YELLOW,
    LEVELS,
    ClinicalEvent,
)

# 等级中文化：直接给前端/文案用，避免各处重复映射
LEVEL_LABELS = {
    LEVEL_RED: "危急",
    LEVEL_ORANGE: "紧急",
    LEVEL_YELLOW: "次紧急",
    LEVEL_GREEN: "常规",
}


def normalize_level(raw: Any) -> str:
    """把模型/外部传入的等级归一化；认不出来的按 red 处理（宁可高估风险）。

    注意：未知值默认 red 是安全侧选择 —— 急症判定宁滥勿缺，
    多提示一次急诊的代价远小于漏掉一次心梗。
    """
    if isinstance(raw, str):
        v = raw.strip().lower()
        if v == "green" or (v in ("低", "low", "none", "normal")):
            return LEVEL_GREEN
        if v in ("yellow", "中", "medium", "mid"):
            return LEVEL_YELLOW
        if v in ("orange", "橙", "高", "high"):
            return LEVEL_ORANGE
        if v in ("red", "红", "critical", "urgent", "emergency", "紧急"):
            return LEVEL_RED
    if raw is None:
        return LEVEL_GREEN
    return LEVEL_RED


def severity(level: str) -> int:
    return LEVEL_SEVERITY.get(level, 0)


def is_emergency_level(level: str) -> bool:
    """red/orange/yellow 都需要离开常规问诊流程。"""
    return level != LEVEL_GREEN


async def record_event(
    db: AsyncSession,
    *,
    event_type: str = EVENT_EMERGENCY,
    level: str,
    reason: str = "",
    symptoms: list[str] | None = None,
    patient_id: int | None = None,
    user_id: str = "",
    session_id: str = "",
    thread_id: str = "",
    trace_id: str = "",
    source: str = "agent",
    payload: dict | None = None,
    commit: bool = True,
    tenant_id: int = 1,
) -> ClinicalEvent:
    """写一条临床事件。调用方传入的字段原样落库，不做业务推导。"""
    deduped: list[str] = []
    for s in symptoms or []:
        s = str(s).strip()
        if s and s not in deduped:
            deduped.append(s)

    event = ClinicalEvent(
        tenant_id=int(tenant_id),
        event_type=event_type,
        level=level if level in LEVELS else LEVEL_RED,
        reason=(reason or "")[:255],
        symptoms=deduped,
        patient_id=patient_id,
        user_id=user_id or "",
        session_id=session_id or "",
        thread_id=thread_id or "",
        trace_id=trace_id or "",
        source=source,
        payload=payload or {},
    )
    db.add(event)
    if commit:
        await db.commit()
        await db.refresh(event)
        # 急症事件发总线：红/橙事件可被告警订阅者（大屏、值班通知）消费，
        # 不必让每个写入点自己再去广播一次。
        from src.infra.event_bus import publish

        await publish(
            "clinical.event.created",
            {
                "event_id": event.id,
                "event_type": event.event_type,
                "level": event.level,
                "patient_id": event.patient_id,
                "session_id": event.session_id,
                "reason": event.reason,
            },
            dedupe_key=str(event.id),
            tenant_id=int(tenant_id),
        )
    return event


async def record_emergency(
    *,
    level: str,
    reason: str = "",
    symptoms: list[str] | None = None,
    patient_id: int | None = None,
    user_id: str = "",
    session_id: str = "",
    thread_id: str = "",
    trace_id: str = "",
    payload: dict | None = None,
    tenant_id: int = 1,
) -> ClinicalEvent | None:
    """在问诊节点里用：自己开 session、自己兜异常。

    节点是"顺带记一笔"的语义，落库失败绝不能打断问诊 —— 少一条统计数据
    可以补，患者拿不到急诊提示不行。
    """
    if not is_emergency_level(level):
        return None
    try:
        from src.infra.database import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            return await record_event(
                db,
                event_type=EVENT_EMERGENCY,
                level=level,
                reason=reason,
                symptoms=symptoms,
                patient_id=patient_id,
                user_id=user_id,
                session_id=session_id,
                thread_id=thread_id,
                trace_id=trace_id,
                source="agent",
                payload=payload,
                tenant_id=tenant_id,
            )
    except Exception as e:  # noqa: BLE001 —— 统计失败不影响主流程
        logger.warning(f"急症事件落库失败（不影响问诊）: {type(e).__name__}: {e}")
        return None


def _as_dict(event: ClinicalEvent) -> dict[str, Any]:
    """对外结构：不含 payload（内部命中文案），只给分级与定位信息。"""
    return {
        "id": event.id,
        "event_type": event.event_type,
        "level": event.level,
        "level_label": LEVEL_LABELS.get(event.level, event.level),
        "reason": event.reason,
        "symptoms": list(event.symptoms or []),
        "patient_id": event.patient_id,
        "session_id": event.session_id,
        "thread_id": event.thread_id,
        "source": event.source,
        "created_at": event.created_at.isoformat() if event.created_at else None,
    }


async def list_events(
    db: AsyncSession,
    *,
    event_type: str = EVENT_EMERGENCY,
    patient_id: int | None = None,
    session_id: str = "",
    level: str | None = None,
    min_level: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """按患者 / 会话 / 等级过滤，按时间倒序。"""
    stmt = select(ClinicalEvent).where(ClinicalEvent.event_type == event_type)
    if patient_id is not None:
        stmt = stmt.where(ClinicalEvent.patient_id == patient_id)
    if session_id:
        stmt = stmt.where(ClinicalEvent.session_id == session_id)
    if level:
        stmt = stmt.where(ClinicalEvent.level == normalize_level(level))
    if min_level:
        floor = severity(normalize_level(min_level))
        allowed = [lv for lv in LEVELS if LEVEL_SEVERITY.get(lv, 0) >= floor]
        stmt = stmt.where(ClinicalEvent.level.in_(allowed))
    stmt = stmt.order_by(ClinicalEvent.id.desc()).limit(max(1, min(limit, 200)))
    rows = (await db.execute(stmt)).scalars().all()
    return [_as_dict(r) for r in rows]


async def count_by_level(
    db: AsyncSession,
    *,
    event_type: str = EVENT_EMERGENCY,
    patient_id: int | None = None,
    session_id: str = "",
) -> dict[str, int]:
    """按等级聚合计数（SQL 聚合，不受列表条数上限影响），四档恒有键。"""
    from sqlalchemy import func

    stmt = (
        select(ClinicalEvent.level, func.count())
        .where(ClinicalEvent.event_type == event_type)
    )
    if patient_id is not None:
        stmt = stmt.where(ClinicalEvent.patient_id == patient_id)
    if session_id:
        stmt = stmt.where(ClinicalEvent.session_id == session_id)
    stmt = stmt.group_by(ClinicalEvent.level)

    counts = {lv: 0 for lv in LEVELS}
    for lv, n in (await db.execute(stmt)).all():
        counts[lv] = int(n)
    return counts

