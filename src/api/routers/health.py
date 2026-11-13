# src/api/routers/health.py
#
# 慢病与健康管理的 REST 接口：用药记录、健康计划、周期健康报告。
#
# 与 Agent 工具的分工：tools/health_tools.py、tools/medication_tools.py 供对话链路读写，
# 这里的接口供前端「健康」页面直接读写同一批表，二者共用 modules 层的业务规则
# （分级 evaluate_metric / 报告生成 build_period_report），避免两套口径。

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_current_user_id
from src.core.exceptions import BizErrorCode, BizException
from src.infra.database import get_db
from src.modules.patient.model import HealthPeriodReport, HealthPlan, MedicationRecord, VitalMetric
from src.modules.patient.schema import HealthPlanIn, HealthPlanUpdate, MedicationIn
from src.modules.patient.service import (
    METRIC_META,
    build_period_report,
    parse_date,
)

router = APIRouter(
    prefix="/api/v1/patient",
    tags=["health-manage"],
    dependencies=[Depends(get_current_user_id)],
)

PERIOD_DAYS = {"week": 7, "month": 30}


def _patient_id(current_user: str) -> int:
    try:
        return int(current_user)
    except (TypeError, ValueError):
        raise BizException(
            BizErrorCode.UNAUTHORIZED, "登录信息无效，请重新登录", status_code=401,
        ) from None


def _audit(action: str, patient_id: int, **extra) -> None:
    logger.bind(audit=True).info(f"health.{action} patient_id={patient_id} {extra or ''}")


def _serialize_medication(m: MedicationRecord) -> dict:
    return {
        "id": m.id,
        "drug_name": m.drug_name,
        "spec": m.spec,
        "dosage": m.dosage,
        "frequency": m.frequency,
        "indication": m.indication,
        "start_date": m.start_date.isoformat() if m.start_date else None,
        "end_date": m.end_date.isoformat() if m.end_date else None,
        "status": m.status,
        "source": m.source,
        "note": m.note,
    }


def _serialize_plan(p: HealthPlan) -> dict:
    return {
        "id": p.id,
        "title": p.title,
        "plan_type": p.plan_type,
        "metric_type": p.metric_type,
        "target_text": p.target_text,
        "frequency": p.frequency,
        "start_date": p.start_date.isoformat() if p.start_date else None,
        "end_date": p.end_date.isoformat() if p.end_date else None,
        "status": p.status,
        "note": p.note,
    }


def _serialize_report(r: HealthPeriodReport) -> dict:
    return {
        "id": r.id,
        "period_type": r.period_type,
        "period_start": r.period_start.isoformat() if r.period_start else None,
        "period_end": r.period_end.isoformat() if r.period_end else None,
        "summary": r.summary,
        "stats": r.stats,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


# ── 用药记录 ──────────────────────────────────────────────────────────────
@router.get("/medications")
async def list_medications(
    status: str | None = None,
    current_user: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """用药台账列表；status 为空返回全部。"""
    patient_id = _patient_id(current_user)
    q = select(MedicationRecord).where(MedicationRecord.patient_id == patient_id)
    if status:
        q = q.where(MedicationRecord.status == status)
    rows = (await db.execute(q.order_by(MedicationRecord.id.desc()).limit(100))).scalars().all()
    return {"medications": [_serialize_medication(m) for m in rows], "total": len(rows)}


@router.post("/medications")
async def create_medication(
    payload: MedicationIn,
    current_user: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    patient_id = _patient_id(current_user)
    record = MedicationRecord(
        patient_id=patient_id,
        drug_name=payload.drug_name,
        spec=payload.spec,
        dosage=payload.dosage,
        frequency=payload.frequency,
        indication=payload.indication,
        start_date=parse_date(payload.start_date) or date.today(),
        end_date=parse_date(payload.end_date),
        status=payload.status,
        source="manual",
        note=payload.note,
    )
    db.add(record)
    await db.commit()
    await db.refresh(record)
    _audit("medication.create", patient_id, status=record.status)
    return {"message": "用药记录已保存", "medication": _serialize_medication(record)}


@router.patch("/medications/{record_id}/stop")
async def stop_medication(
    record_id: int,
    current_user: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """停用在用药品：status → stopped，并补结束日期。"""
    patient_id = _patient_id(current_user)
    record = await db.get(MedicationRecord, record_id)
    if record is None or record.patient_id != patient_id:
        raise BizException(BizErrorCode.NOT_FOUND, "用药记录不存在", status_code=404)
    record.status = "stopped"
    record.end_date = record.end_date or date.today()
    await db.commit()
    _audit("medication.stop", patient_id, record_id=record_id)
    return {"message": "已停用", "medication": _serialize_medication(record)}


@router.delete("/medications/{record_id}")
async def delete_medication(
    record_id: int,
    current_user: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    patient_id = _patient_id(current_user)
    record = await db.get(MedicationRecord, record_id)
    if record is None or record.patient_id != patient_id:
        raise BizException(BizErrorCode.NOT_FOUND, "用药记录不存在", status_code=404)
    await db.delete(record)
    await db.commit()
    _audit("medication.delete", patient_id, record_id=record_id)
    return {"message": "用药记录已删除"}


# ── 健康计划 ──────────────────────────────────────────────────────────────
@router.get("/plans")
async def list_plans(
    status: str | None = None,
    current_user: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    patient_id = _patient_id(current_user)
    q = select(HealthPlan).where(HealthPlan.patient_id == patient_id)
    if status:
        q = q.where(HealthPlan.status == status)
    rows = (await db.execute(q.order_by(HealthPlan.id.desc()).limit(100))).scalars().all()
    return {"plans": [_serialize_plan(p) for p in rows], "total": len(rows)}


@router.post("/plans")
async def create_plan(
    payload: HealthPlanIn,
    current_user: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    patient_id = _patient_id(current_user)
    plan = HealthPlan(
        patient_id=patient_id,
        title=payload.title,
        plan_type=payload.plan_type,
        metric_type=payload.metric_type,
        target_text=payload.target_text,
        frequency=payload.frequency,
        start_date=parse_date(payload.start_date) or date.today(),
        end_date=parse_date(payload.end_date),
        status="active",
        note=payload.note,
    )
    db.add(plan)
    await db.commit()
    await db.refresh(plan)
    _audit("plan.create", patient_id)
    return {"message": "健康计划已创建", "plan": _serialize_plan(plan)}


@router.patch("/plans/{plan_id}")
async def update_plan(
    plan_id: int,
    payload: HealthPlanUpdate,
    current_user: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    patient_id = _patient_id(current_user)
    plan = await db.get(HealthPlan, plan_id)
    if plan is None or plan.patient_id != patient_id:
        raise BizException(BizErrorCode.NOT_FOUND, "健康计划不存在", status_code=404)
    for field in ("title", "target_text", "frequency", "status", "note"):
        value = getattr(payload, field)
        if value is not None:
            setattr(plan, field, value)
    if payload.end_date:
        plan.end_date = parse_date(payload.end_date)
    await db.commit()
    _audit("plan.update", patient_id, plan_id=plan_id, status=plan.status)
    return {"message": "已更新", "plan": _serialize_plan(plan)}


# ── 周期健康报告 ──────────────────────────────────────────────────────────
@router.get("/period-reports")
async def list_period_reports(
    limit: int = 12,
    current_user: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """历史周期报告列表（只返回摘要与统计，正文按需取详情）。"""
    patient_id = _patient_id(current_user)
    rows = (
        await db.execute(
            select(HealthPeriodReport)
            .where(HealthPeriodReport.patient_id == patient_id)
            .order_by(HealthPeriodReport.period_end.desc())
            .limit(max(1, min(limit, 50)))
        )
    ).scalars().all()
    return {"reports": [_serialize_report(r) for r in rows], "total": len(rows)}


@router.get("/period-reports/{report_id}")
async def get_period_report(
    report_id: int,
    current_user: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    patient_id = _patient_id(current_user)
    report = await db.get(HealthPeriodReport, report_id)
    if report is None or report.patient_id != patient_id:
        raise BizException(BizErrorCode.NOT_FOUND, "报告不存在", status_code=404)
    return {"report": {**_serialize_report(report), "content": report.content}}


@router.post("/period-reports/generate")
async def generate_period_report(
    period_type: str = "week",
    current_user: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """按周/月生成周期健康报告（规则化统计，不调 LLM，保证可复现）。"""
    patient_id = _patient_id(current_user)
    ptype = (period_type or "week").lower()
    if ptype not in PERIOD_DAYS:
        raise BizException(BizErrorCode.PARAM_ERROR, "周期只支持 week 或 month", status_code=400)

    end = date.today()
    start = end - timedelta(days=PERIOD_DAYS[ptype] - 1)
    since = datetime.now(timezone.utc) - timedelta(days=PERIOD_DAYS[ptype])

    metrics = (
        await db.execute(
            select(VitalMetric)
            .where(VitalMetric.patient_id == patient_id, VitalMetric.measured_at >= since)
            .order_by(VitalMetric.measured_at.asc())
        )
    ).scalars().all()
    plans = (
        await db.execute(select(HealthPlan).where(HealthPlan.patient_id == patient_id).limit(20))
    ).scalars().all()
    meds = (
        await db.execute(
            select(MedicationRecord)
            .where(MedicationRecord.patient_id == patient_id)
            .order_by(MedicationRecord.id.desc())
            .limit(20)
        )
    ).scalars().all()

    summary, content, stats = build_period_report(
        ptype,
        start,
        end,
        [
            {
                "metric_type": m.metric_type,
                "primary": m.value_primary,
                "secondary": m.value_secondary,
                "unit": m.unit or METRIC_META.get(m.metric_type, {}).get("unit", ""),
                "level": m.level,
            }
            for m in metrics
        ],
        [{"title": p.title, "status": p.status, "target_text": p.target_text} for p in plans],
        [
            {"drug_name": m.drug_name, "dosage": m.dosage, "frequency": m.frequency, "status": m.status}
            for m in meds
        ],
    )

    report = HealthPeriodReport(
        patient_id=patient_id,
        period_type=ptype,
        period_start=start,
        period_end=end,
        summary=summary,
        content=content,
        stats=stats,
    )
    db.add(report)
    await db.commit()
    await db.refresh(report)
    _audit("period_report.generate", patient_id, period=ptype)
    return {"message": "报告已生成", "report": {**_serialize_report(report), "content": content}}
