# src/api/routers/patient.py
#
# 患者端个人中心 API：首页概览、健康档案、体征指标、健康提醒、隐私授权、
# 数据导出与账号注销申请。
#
# 安全约定：
# - patient_id 只从 JWT sub 解析（get_current_user_id），任何 body/query 自报 ID 一律忽略
# - 所有查询都带 patient_id == 当前用户，避免越权读取他人档案
# - 日志只记动作与计数，不记指标值/档案原文

from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Response
from fastapi.responses import StreamingResponse
from loguru import logger
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_current_user_id, require_scope
from src.core.config import get_settings
from src.core.concurrency import to_thread
from src.core.exceptions import BizErrorCode, BizException
from src.core.llm_guard import scrub_for_llm
from src.core.redaction import redact_text
from src.infra.database import AsyncSessionLocal, get_db
from src.infra.rustfs_client import aupload_file, delete_object, download_file
from src.modules.medical.model import Appointment, Consultation, Patient
from src.modules.patient.model import (
    DataRequest,
    HealthReminder,
    HealthReport,
    PatientProfile,
    PrivacyConsent,
    ReportMetric,
    VitalMetric,
)
from src.api.routers.sse import sse_frame
from src.modules.patient.metric_advice import MetricSnapshot, stream_advice
from src.modules.patient.schema import (
    ConsentUpdate,
    MetricIn,
    ProfileUpdate,
    ReminderIn,
    ReminderUpdate,
)
from src.modules.patient.service import (
    LEVEL_LABELS,
    METRIC_META,
    bmi_level,
    calc_bmi,
    evaluate_metric,
    metric_label,
    parse_date,
    parse_datetime,
)

# router 级守卫作为兜底：新增端点即使漏写 Depends，也不会暴露患者数据
router = APIRouter(
    prefix="/api/v1/patient",
    tags=["patient"],
    dependencies=[Depends(get_current_user_id), Depends(require_scope("patient:read"))],
)

settings = get_settings()

# 隐私授权默认值：基础 AI 咨询默认开启，其余需显式授权
_DEFAULT_CONSENTS = {"ai_consult": True, "data_share": False, "research": False, "marketing": False}
_CONSENT_LABELS = {
    "ai_consult": "AI 健康咨询",
    "data_share": "向就诊医生共享健康档案",
    "research": "匿名化数据用于医学研究",
    "marketing": "健康活动与产品推荐",
}


def _patient_id(current_user: str) -> int:
    try:
        return int(current_user)
    except (TypeError, ValueError):
        raise BizException(
            BizErrorCode.UNAUTHORIZED, "登录信息无效，请重新登录", status_code=401,
        ) from None


async def _get_patient(db: AsyncSession, patient_id: int) -> Patient:
    patient = (await db.execute(select(Patient).where(Patient.id == patient_id))).scalar_one_or_none()
    if patient is None:
        raise BizException(BizErrorCode.NOT_FOUND, "患者档案不存在", status_code=404)
    return patient


async def _get_or_create_profile(db: AsyncSession, patient_id: int) -> PatientProfile:
    profile = (
        await db.execute(select(PatientProfile).where(PatientProfile.patient_id == patient_id))
    ).scalar_one_or_none()
    if profile is None:
        profile = PatientProfile(patient_id=patient_id)
        db.add(profile)
        await db.flush()
    return profile


def _serialize_metric(m: VitalMetric) -> dict:
    meta = METRIC_META.get(m.metric_type, {})
    return {
        "id": m.id,
        "metric_type": m.metric_type,
        "label": metric_label(m.metric_type),
        "value_primary": m.value_primary,
        "value_secondary": m.value_secondary,
        "unit": m.unit or meta.get("unit", ""),
        "period": m.period,
        "level": m.level,
        "level_label": LEVEL_LABELS.get(m.level, m.level),
        "source": m.source,
        "note": m.note,
        "measured_at": m.measured_at.isoformat() if m.measured_at else None,
    }


def _profile_completeness(patient: Patient, profile: PatientProfile) -> int:
    fields = [
        patient.name, patient.gender, patient.age, patient.blood_type,
        patient.allergy_history, patient.medical_history,
        profile.height_cm, profile.weight_kg, profile.birth_date,
        profile.surgery_history, profile.family_history, profile.emergency_contact_name,
    ]
    filled = sum(1 for f in fields if f not in (None, "", 0))
    return int(round(filled / len(fields) * 100))


def _consent_map(rows: list[PrivacyConsent]) -> dict:
    latest: dict[str, PrivacyConsent] = {}
    for row in rows:  # 已按 id 升序，后者覆盖前者
        latest[row.scope] = row
    result = []
    for scope, default in _DEFAULT_CONSENTS.items():
        row = latest.get(scope)
        result.append({
            "scope": scope,
            "label": _CONSENT_LABELS.get(scope, scope),
            "granted": row.granted if row else default,
            "version": row.version if row else "v1",
            "granted_at": row.granted_at.isoformat() if row and row.granted_at else None,
            "revoked_at": row.revoked_at.isoformat() if row and row.revoked_at else None,
        })
    return {"consents": result}


def _audit(action: str, patient_id: int, **extra) -> None:
    """最小化审计日志：只记动作与计数，不含健康数据。"""
    logger.bind(audit=True).info(f"patient.{action} patient_id={patient_id} {extra or ''}")


# ── 首页概览 ──────────────────────────────────────────────────────────────
@router.get("/overview")
async def overview(
    current_user: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """首页：健康概览 + 入口所需计数。"""
    patient_id = _patient_id(current_user)
    patient = await _get_patient(db, patient_id)
    profile = await _get_or_create_profile(db, patient_id)

    metrics = (
        await db.execute(
            select(VitalMetric)
            .where(VitalMetric.patient_id == patient_id)
            .order_by(VitalMetric.measured_at.desc())
            .limit(200)
        )
    ).scalars().all()
    latest: dict[str, dict] = {}
    for m in metrics:
        if m.metric_type not in latest:
            latest[m.metric_type] = _serialize_metric(m)

    reports = (
        await db.execute(
            select(HealthReport)
            .where(HealthReport.patient_id == patient_id, HealthReport.deleted_at.is_(None))
            .order_by(HealthReport.created_at.desc())
            .limit(3)
        )
    ).scalars().all()
    abnormal_reports = (
        await db.execute(
            select(func.count())
            .select_from(HealthReport)
            .where(
                HealthReport.patient_id == patient_id,
                HealthReport.deleted_at.is_(None),
                HealthReport.abnormal_count > 0,
            )
        )
    ).scalar() or 0
    reports_total = (
        await db.execute(
            select(func.count())
            .select_from(HealthReport)
            .where(HealthReport.patient_id == patient_id, HealthReport.deleted_at.is_(None))
        )
    ).scalar() or 0
    consultations_total = (
        await db.execute(
            select(func.count()).select_from(Consultation).where(Consultation.patient_id == patient_id)
        )
    ).scalar() or 0

    reminders = (
        await db.execute(
            select(HealthReminder).where(
                HealthReminder.patient_id == patient_id, HealthReminder.enabled.is_(True)
            )
        )
    ).scalars().all()
    today = date.today().isoformat()
    today_reminders = [
        {"id": r.id, "title": r.title, "remind_time": r.remind_time, "metric_type": r.metric_type}
        for r in reminders
        if r.frequency == "daily"
        or (r.weekdays and str(date.today().isoweekday()) in r.weekdays.split(","))
    ]
    today_reminders.sort(key=lambda x: x["remind_time"] or "")

    next_appt = (
        await db.execute(
            select(Appointment)
            .where(
                Appointment.patient_id == patient_id,
                Appointment.status == "confirmed",
                Appointment.date >= today,
            )
            .order_by(Appointment.date.asc())
            .limit(1)
        )
    ).scalar_one_or_none()

    latest_weight = next(
        (m for m in metrics if m.metric_type == "weight"), None
    )
    bmi = calc_bmi(profile.height_cm, latest_weight.value_primary if latest_weight else profile.weight_kg)

    risk_alerts = [
        {
            "metric_type": v["metric_type"],
            "label": v["label"],
            "level": v["level"],
            "message": f"{v['label']}{LEVEL_LABELS.get(v['level'], v['level'])}，请关注",
        }
        for v in latest.values()
        if v["level"] in ("high", "critical", "low")
    ]

    _audit("overview", patient_id)
    return {
        "profile": {
            "name": patient.name,
            "gender": patient.gender,
            "age": patient.age,
            "blood_type": patient.blood_type,
            "completeness": _profile_completeness(patient, profile),
        },
        "bmi": bmi,
        "bmi_level": bmi_level(bmi),
        "latest_metrics": latest,
        "risk_alerts": risk_alerts,
        "reports": [
            {
                "id": r.id,
                "file_name": r.file_name,
                "report_type": r.report_type,
                "status": r.status,
                "abnormal_count": r.abnormal_count,
                "metric_count": r.metric_count,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in reports
        ],
        "reports_total": reports_total,
        "abnormal_reports": abnormal_reports,
        "consultations_total": consultations_total,
        "today_reminders": today_reminders,
        "next_appointment": (
            {
                "appointment_no": next_appt.appointment_no,
                "department": next_appt.department,
                "doctor_name": next_appt.doctor_name,
                "date": next_appt.date,
                "time_slot": next_appt.time_slot,
            }
            if next_appt else None
        ),
    }


# ── 健康档案 ──────────────────────────────────────────────────────────────
@router.get("/profile")
async def get_profile(
    current_user: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    patient_id = _patient_id(current_user)
    patient = await _get_patient(db, patient_id)
    profile = await _get_or_create_profile(db, patient_id)
    consents = (
        await db.execute(
            select(PrivacyConsent)
            .where(PrivacyConsent.patient_id == patient_id)
            .order_by(PrivacyConsent.id.asc())
        )
    ).scalars().all()

    bmi = calc_bmi(profile.height_cm, profile.weight_kg)
    _audit("profile.read", patient_id)
    return {
        "patient_id": patient.id,
        "name": patient.name,
        "gender": patient.gender,
        "age": patient.age,
        "phone_masked": _mask_phone(patient.phone),
        "birth_date": profile.birth_date.isoformat() if profile.birth_date else None,
        "blood_type": patient.blood_type,
        "height_cm": profile.height_cm,
        "weight_kg": profile.weight_kg,
        "waist_cm": profile.waist_cm,
        "bmi": bmi,
        "bmi_level": bmi_level(bmi),
        "marital_status": profile.marital_status,
        "occupation": profile.occupation,
        "city": profile.city,
        "emergency_contact_name": profile.emergency_contact_name,
        "emergency_contact_phone_masked": _mask_phone(profile.emergency_contact_phone),
        "allergy_history": patient.allergy_history,
        "medical_history": patient.medical_history,
        "surgery_history": profile.surgery_history,
        "family_history": profile.family_history,
        "chronic_diseases": profile.chronic_diseases,
        "smoking": profile.smoking,
        "drinking": profile.drinking,
        "completeness": _profile_completeness(patient, profile),
        "consents": _consent_map(consents)["consents"],
    }


def _mask_phone(phone: str | None) -> str:
    if not phone:
        return ""
    if len(phone) <= 4:
        return "*" * len(phone)
    return f"{phone[:3]}****{phone[-4:]}"


@router.put("/profile")
async def update_profile(
    payload: ProfileUpdate,
    current_user: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    patient_id = _patient_id(current_user)
    patient = await _get_patient(db, patient_id)
    profile = await _get_or_create_profile(db, patient_id)

    data = payload.model_dump(exclude_unset=True)
    # 基础身份字段写 patients
    for field in ("name", "gender", "age", "blood_type", "allergy_history", "medical_history"):
        if field in data:
            setattr(patient, field, data[field])
    # 扩展字段写 patient_profiles
    for field in (
        "height_cm", "weight_kg", "waist_cm", "marital_status", "occupation", "city",
        "emergency_contact_name", "emergency_contact_phone", "surgery_history",
        "family_history", "chronic_diseases", "smoking", "drinking",
    ):
        if field in data:
            setattr(profile, field, data[field])
    if "birth_date" in data:
        profile.birth_date = parse_date(data["birth_date"])

    await db.commit()
    _audit("profile.write", patient_id, fields=len(data))
    return {"message": "健康档案已保存", "completeness": _profile_completeness(patient, profile)}


# ── 体征指标 ──────────────────────────────────────────────────────────────
@router.get("/metrics")
async def list_metrics(
    metric_type: str | None = None,
    days: int = 30,
    limit: int = 200,
    current_user: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    patient_id = _patient_id(current_user)
    since = datetime.now(timezone.utc) - timedelta(days=max(1, min(days, 365)))
    q = (
        select(VitalMetric)
        .where(VitalMetric.patient_id == patient_id, VitalMetric.measured_at >= since)
        .order_by(VitalMetric.measured_at.desc())
        .limit(max(1, min(limit, 500)))
    )
    if metric_type:
        q = q.where(VitalMetric.metric_type == metric_type)
    rows = (await db.execute(q)).scalars().all()
    return {"metrics": [_serialize_metric(m) for m in rows], "total": len(rows)}


@router.post("/metrics")
async def create_metric(
    payload: MetricIn,
    current_user: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    patient_id = _patient_id(current_user)
    if payload.metric_type == "blood_pressure" and payload.value_secondary is None:
        raise BizException(BizErrorCode.PARAM_ERROR, "血压记录需要同时填写舒张压", status_code=400)

    level = evaluate_metric(
        payload.metric_type, payload.value_primary, payload.value_secondary, payload.period
    )
    measured_at = parse_datetime(payload.measured_at) or datetime.now(timezone.utc)
    metric = VitalMetric(
        patient_id=patient_id,
        metric_type=payload.metric_type,
        value_primary=payload.value_primary,
        value_secondary=payload.value_secondary,
        unit=payload.unit or METRIC_META.get(payload.metric_type, {}).get("unit", ""),
        period=payload.period,
        level=level,
        source="manual",
        note=payload.note,
        measured_at=measured_at,
    )
    db.add(metric)
    await db.commit()
    await db.refresh(metric)
    _audit("metric.create", patient_id, type=payload.metric_type, level=level)
    return {"message": "记录成功", "metric": _serialize_metric(metric)}


@router.get("/metrics/trends")
async def metric_trends(
    days: int = 30,
    current_user: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """趋势聚合：每个指标类型一条时间序列 + 均值/极值。"""
    patient_id = _patient_id(current_user)
    since = datetime.now(timezone.utc) - timedelta(days=max(1, min(days, 365)))
    rows = (
        await db.execute(
            select(VitalMetric)
            .where(VitalMetric.patient_id == patient_id, VitalMetric.measured_at >= since)
            .order_by(VitalMetric.measured_at.asc())
        )
    ).scalars().all()

    series: dict[str, list[dict]] = {}
    for m in rows:
        series.setdefault(m.metric_type, []).append(
            {
                "t": m.measured_at.isoformat() if m.measured_at else None,
                "primary": m.value_primary,
                "secondary": m.value_secondary,
                "level": m.level,
            }
        )

    result = []
    for metric_type, points in series.items():
        primaries = [p["primary"] for p in points]
        result.append({
            "metric_type": metric_type,
            "label": metric_label(metric_type),
            "unit": METRIC_META.get(metric_type, {}).get("unit", ""),
            "points": points,
            "count": len(points),
            "avg": round(sum(primaries) / len(primaries), 1),
            "max": max(primaries),
            "min": min(primaries),
            "latest": points[-1],
            "abnormal_count": sum(1 for p in points if p["level"] not in ("normal",)),
        })
    return {"trends": result, "days": days}


@router.post("/metrics/{metric_id}/advice")
async def metric_risk_advice(
    metric_id: int,
    current_user: str = Depends(get_current_user_id),
):
    """单条体征记录的风险解读（SSE 流式）：知识库检索 + 分段建议。

    只接受记录 ID，数值以库里的记录为准（避免前端自报数值被拿来当"体检结论"）；
    知识库/模型任何环节失败都会降级成通用建议，不向前端抛 5xx。

    这里刻意不用 Depends(get_db)：请求级 session 会一直持有连接直到流结束，
    用户中途关闭面板（断连）时任务被取消，连接在取消作用域里被 terminate，
    asyncpg 会打出 "Exception terminating connection"。改为短会话取快照后立即归还。
    """
    patient_id = _patient_id(current_user)
    snapshot = await _build_advice_snapshot(metric_id, patient_id)
    if snapshot is None:
        raise BizException(BizErrorCode.NOT_FOUND, "记录不存在", status_code=404)

    _audit("metric.advice", patient_id, metric_id=metric_id, type=snapshot.metric_type)

    async def _events():
        async for event in stream_advice(snapshot):
            yield sse_frame(event)

    return StreamingResponse(
        _events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _build_advice_snapshot(metric_id: int, patient_id: int):
    """在一个短会话里取齐解读所需数据，出函数即释放连接。"""
    async with AsyncSessionLocal() as db:
        metric = (
            await db.execute(
                select(VitalMetric).where(
                    VitalMetric.id == metric_id, VitalMetric.patient_id == patient_id
                )
            )
        ).scalar_one_or_none()
        if metric is None:
            return None

        patient = await db.get(Patient, patient_id)
        profile = (
            await db.execute(
                select(PatientProfile).where(PatientProfile.patient_id == patient_id)
            )
        ).scalar_one_or_none()

        # 近 30 天同类型记录：让模型能对比趋势，而不是只看一个孤立数值
        since = datetime.now(timezone.utc) - timedelta(days=30)
        recent = (
            await db.execute(
                select(VitalMetric)
                .where(
                    VitalMetric.patient_id == patient_id,
                    VitalMetric.metric_type == metric.metric_type,
                    VitalMetric.measured_at >= since,
                )
                .order_by(VitalMetric.measured_at.desc())
                .limit(10)
            )
        ).scalars().all()

        return MetricSnapshot(
            metric_id=metric.id,
            metric_type=metric.metric_type,
            value_primary=float(metric.value_primary),
            value_secondary=(
                float(metric.value_secondary) if metric.value_secondary is not None else None
            ),
            unit=metric.unit or "",
            level=metric.level or "normal",
            period=metric.period,
            gender=getattr(patient, "gender", None),
            # 年龄由 metric_advice._age_band 区间化后再入 prompt；这里只做慢性病文本脱敏
            age=getattr(patient, "age", None),
            height_cm=getattr(profile, "height_cm", None),
            weight_kg=getattr(profile, "weight_kg", None),
            chronic_diseases=scrub_for_llm(getattr(profile, "chronic_diseases", None)) or None,
            recent_primary=[float(m.value_primary) for m in recent if m.value_primary is not None],
        )


@router.delete("/metrics/{metric_id}")
async def delete_metric(
    metric_id: int,
    current_user: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    patient_id = _patient_id(current_user)
    result = await db.execute(
        delete(VitalMetric).where(
            VitalMetric.id == metric_id, VitalMetric.patient_id == patient_id
        )
    )
    await db.commit()
    if not result.rowcount:
        raise BizException(BizErrorCode.NOT_FOUND, "记录不存在", status_code=404)
    _audit("metric.delete", patient_id, metric_id=metric_id)
    return {"message": "已删除"}


# ── 健康提醒 ──────────────────────────────────────────────────────────────
def _serialize_reminder(r: HealthReminder) -> dict:
    return {
        "id": r.id,
        "title": r.title,
        "metric_type": r.metric_type,
        "remind_time": r.remind_time,
        "frequency": r.frequency,
        "weekdays": r.weekdays,
        "enabled": r.enabled,
        "note": r.note,
    }


@router.get("/reminders")
async def list_reminders(
    current_user: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    patient_id = _patient_id(current_user)
    rows = (
        await db.execute(
            select(HealthReminder)
            .where(HealthReminder.patient_id == patient_id)
            .order_by(HealthReminder.remind_time.asc())
        )
    ).scalars().all()
    return {"reminders": [_serialize_reminder(r) for r in rows], "total": len(rows)}


@router.post("/reminders")
async def create_reminder(
    payload: ReminderIn,
    current_user: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    patient_id = _patient_id(current_user)
    reminder = HealthReminder(patient_id=patient_id, **payload.model_dump())
    db.add(reminder)
    await db.commit()
    await db.refresh(reminder)
    _audit("reminder.create", patient_id)
    return {"message": "提醒已创建", "reminder": _serialize_reminder(reminder)}


@router.put("/reminders/{reminder_id}")
async def update_reminder(
    reminder_id: int,
    payload: ReminderUpdate,
    current_user: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    patient_id = _patient_id(current_user)
    reminder = (
        await db.execute(
            select(HealthReminder).where(
                HealthReminder.id == reminder_id, HealthReminder.patient_id == patient_id
            )
        )
    ).scalar_one_or_none()
    if reminder is None:
        raise BizException(BizErrorCode.NOT_FOUND, "提醒不存在", status_code=404)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(reminder, field, value)
    await db.commit()
    return {"message": "已更新", "reminder": _serialize_reminder(reminder)}


@router.delete("/reminders/{reminder_id}")
async def delete_reminder(
    reminder_id: int,
    current_user: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    patient_id = _patient_id(current_user)
    result = await db.execute(
        delete(HealthReminder).where(
            HealthReminder.id == reminder_id, HealthReminder.patient_id == patient_id
        )
    )
    await db.commit()
    if not result.rowcount:
        raise BizException(BizErrorCode.NOT_FOUND, "提醒不存在", status_code=404)
    return {"message": "已删除"}


# ── 隐私授权 ──────────────────────────────────────────────────────────────
@router.get("/privacy/consents")
async def get_consents(
    current_user: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    patient_id = _patient_id(current_user)
    rows = (
        await db.execute(
            select(PrivacyConsent)
            .where(PrivacyConsent.patient_id == patient_id)
            .order_by(PrivacyConsent.id.asc())
        )
    ).scalars().all()
    return _consent_map(rows)


@router.put("/privacy/consents")
async def update_consent(
    payload: ConsentUpdate,
    current_user: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    patient_id = _patient_id(current_user)
    now = datetime.now(timezone.utc)
    db.add(
        PrivacyConsent(
            patient_id=patient_id,
            scope=payload.scope,
            granted=payload.granted,
            version=payload.version,
            granted_at=now if payload.granted else None,
            revoked_at=None if payload.granted else now,
        )
    )
    await db.commit()
    _audit("consent.update", patient_id, scope=payload.scope, granted=payload.granted)
    return {"message": "授权设置已更新"}


# ── 数据导出 / 删除 ───────────────────────────────────────────────────────
@router.post("/privacy/export")
async def request_export(
    current_user: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """导出本人全部健康数据（JSON），文件放 RustFS，30 天后过期。"""
    patient_id = _patient_id(current_user)
    patient = await _get_patient(db, patient_id)
    profile = await _get_or_create_profile(db, patient_id)

    metrics = (
        await db.execute(
            select(VitalMetric).where(VitalMetric.patient_id == patient_id).order_by(VitalMetric.measured_at.asc())
        )
    ).scalars().all()
    reports = (
        await db.execute(
            select(HealthReport).where(
                HealthReport.patient_id == patient_id, HealthReport.deleted_at.is_(None)
            )
        )
    ).scalars().all()
    report_ids = [r.id for r in reports]
    report_metrics = (
        await db.execute(select(ReportMetric).where(ReportMetric.report_id.in_(report_ids)))
    ).scalars().all() if report_ids else []
    reminders = (
        await db.execute(select(HealthReminder).where(HealthReminder.patient_id == patient_id))
    ).scalars().all()
    consents = (
        await db.execute(select(PrivacyConsent).where(PrivacyConsent.patient_id == patient_id))
    ).scalars().all()
    consultations = (
        await db.execute(select(Consultation).where(Consultation.patient_id == patient_id))
    ).scalars().all()

    payload = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "patient": {
            "id": patient.id,
            "name": patient.name,
            "gender": patient.gender,
            "age": patient.age,
            "blood_type": patient.blood_type,
            "allergy_history": patient.allergy_history,
            "medical_history": patient.medical_history,
        },
        "profile": {
            "birth_date": profile.birth_date.isoformat() if profile.birth_date else None,
            "height_cm": profile.height_cm,
            "weight_kg": profile.weight_kg,
            "waist_cm": profile.waist_cm,
            "surgery_history": profile.surgery_history,
            "family_history": profile.family_history,
            "chronic_diseases": profile.chronic_diseases,
            "smoking": profile.smoking,
            "drinking": profile.drinking,
        },
        "metrics": [_serialize_metric(m) for m in metrics],
        "reports": [
            {
                "id": r.id,
                "file_name": r.file_name,
                "report_type": r.report_type,
                "report_date": r.report_date.isoformat() if r.report_date else None,
                "summary": r.summary,
                "metrics": [
                    {
                        "name": rm.name,
                        "value": rm.value_text,
                        "unit": rm.unit,
                        "ref": rm.ref_text,
                        "abnormal": rm.abnormal,
                    }
                    for rm in report_metrics
                    if rm.report_id == r.id
                ],
            }
            for r in reports
        ],
        "reminders": [_serialize_reminder(r) for r in reminders],
        "consents": [
            {"scope": c.scope, "granted": c.granted, "version": c.version} for c in consents
        ],
        "consultations": [
            {
                "session_id": c.session_id,
                "chief_complaint": c.chief_complaint,
                "diagnosis": c.diagnosis,
                "created_at": c.created_at.isoformat() if c.created_at else None,
            }
            for c in consultations
        ],
    }

    content = json.dumps(payload, ensure_ascii=False, default=str, indent=2).encode("utf-8")
    key = f"patient-exports/{patient_id}/{uuid.uuid4().hex}.json"
    upload_error = ""
    try:
        await aupload_file(key, content, "application/json")
    except Exception as e:
        # 对象存储不可用时仍然把申请落库，便于人工补发
        upload_error = redact_text(str(e), 120)
        logger.warning(f"导出文件上传失败 patient_id={patient_id}: {upload_error}")

    expires_at = datetime.now(timezone.utc) + timedelta(days=30)
    req = DataRequest(
        patient_id=patient_id,
        request_type="export",
        status="completed" if not upload_error else "failed",
        object_key=key if not upload_error else None,
        note=f"导出 {len(metrics)} 条指标 / {len(reports)} 份报告",
        expires_at=expires_at,
        handled_at=datetime.now(timezone.utc),
    )
    db.add(req)
    await db.commit()
    await db.refresh(req)
    _audit("export", patient_id, request_id=req.id)

    return {
        "message": "数据导出已生成，30 天内可下载" if not upload_error else "导出文件生成失败，请稍后重试",
        "request_id": req.id,
        "status": req.status,
        "expires_at": expires_at.isoformat(),
        "download_url": f"/api/v1/patient/privacy/export/{req.id}/download" if not upload_error else None,
    }


@router.get("/privacy/export/{request_id}/download")
async def download_export(
    request_id: int,
    current_user: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    patient_id = _patient_id(current_user)
    req = (
        await db.execute(
            select(DataRequest).where(
                DataRequest.id == request_id,
                DataRequest.patient_id == patient_id,
                DataRequest.request_type == "export",
            )
        )
    ).scalar_one_or_none()
    if req is None or not req.object_key:
        raise BizException(BizErrorCode.NOT_FOUND, "导出文件不存在", status_code=404)
    expires_at = req.expires_at
    if expires_at is not None and expires_at.tzinfo is None:
        # 部分驱动回传 naive datetime（列本身是 timestamptz），统一按 UTC 比较
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at and expires_at < datetime.now(timezone.utc):
        raise BizException(BizErrorCode.PARAM_ERROR, "导出文件已过期，请重新申请", status_code=410)
    try:
        content = await to_thread(download_file, req.object_key)
    except Exception:
        logger.exception(f"导出文件下载失败 patient_id={patient_id} request_id={request_id}")
        raise BizException(BizErrorCode.INTERNAL, "导出文件暂时不可用", status_code=500) from None
    _audit("export.download", patient_id, request_id=request_id)
    return Response(
        content=content,
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="health-data-{request_id}.json"',
            "Cache-Control": "no-store",
        },
    )


@router.post("/privacy/delete")
async def request_delete(
    current_user: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """账号注销 / 数据删除申请：医疗数据需人工核验后按保留期处理，不自动物理删除。"""
    patient_id = _patient_id(current_user)
    existing = (
        await db.execute(
            select(DataRequest).where(
                DataRequest.patient_id == patient_id,
                DataRequest.request_type == "delete",
                DataRequest.status.in_(("pending", "processing")),
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return {"message": "删除申请已受理，正在处理中", "request_id": existing.id, "status": existing.status}

    req = DataRequest(
        patient_id=patient_id,
        request_type="delete",
        status="pending",
        note="用户申请注销账号并删除健康数据，需人工核验身份后处理（医疗记录依法保留）",
    )
    db.add(req)
    # 撤回全部授权
    for scope in _DEFAULT_CONSENTS:
        db.add(
            PrivacyConsent(
                patient_id=patient_id, scope=scope, granted=False,
                version="v1", revoked_at=datetime.now(timezone.utc),
            )
        )
    await db.commit()
    await db.refresh(req)
    _audit("delete.request", patient_id, request_id=req.id)
    return {
        "message": "已受理删除申请。我们将在 7 个工作日内完成身份核验与处理，结果会通过手机号通知。",
        "request_id": req.id,
        "status": "pending",
    }


@router.get("/privacy/requests")
async def list_data_requests(
    current_user: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    patient_id = _patient_id(current_user)
    rows = (
        await db.execute(
            select(DataRequest)
            .where(DataRequest.patient_id == patient_id)
            .order_by(DataRequest.created_at.desc())
            .limit(20)
        )
    ).scalars().all()
    return {
        "requests": [
            {
                "id": r.id,
                "request_type": r.request_type,
                "status": r.status,
                "note": r.note,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "expires_at": r.expires_at.isoformat() if r.expires_at else None,
            }
            for r in rows
        ]
    }
