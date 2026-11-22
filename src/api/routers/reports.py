# src/api/routers/reports.py
#
# 患者端报告中心：上传 → 解析 → 详情 → 指标对比。
#
# 隐私约束：
# - 报告原文只存 RustFS，DB 只存结构化指标与规则化摘要
# - 解析失败原因写库前先脱敏，日志不输出文件名以外的报告内容
# - 所有查询强制 patient_id == 当前登录患者

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, Form, UploadFile
from loguru import logger
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import AuthContext, get_current_user_id, require_auth, require_scope
from src.core.concurrency import to_thread
from src.core.exceptions import BizErrorCode, BizException
from src.core.redaction import redact_text
from src.infra.database import get_db
from src.infra.rustfs_client import aupload_file, delete_object, download_file
from src.modules.medical.model import Patient
from src.modules.patient.model import HealthReport, ReportMetric
from src.modules.patient.report_parser import parse_report
from src.modules.patient.service import summarize_report

router = APIRouter(
    prefix="/api/v1/patient/reports",
    tags=["patient-reports"],
    dependencies=[Depends(get_current_user_id), Depends(require_scope("report:read"))],
)

MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 20MB
ALLOWED_EXT = {".pdf", ".jpg", ".jpeg", ".png", ".webp"}
ALLOWED_REPORT_TYPES = {"lab", "imaging", "physical", "other"}


def _patient_id(current_user: str) -> int:
    try:
        return int(current_user)
    except (TypeError, ValueError):
        raise BizException(BizErrorCode.UNAUTHORIZED, "登录信息无效，请重新登录", status_code=401) from None


def _audit(action: str, patient_id: int, **extra) -> None:
    logger.bind(audit=True).info(f"report.{action} patient_id={patient_id} {extra or ''}")


def _serialize_metric(m: ReportMetric) -> dict:
    return {
        "id": m.id,
        "name": m.name,
        "code": m.code,
        "category": m.category,
        "value_text": m.value_text,
        "value_num": m.value_num,
        "unit": m.unit,
        "ref_low": m.ref_low,
        "ref_high": m.ref_high,
        "ref_text": m.ref_text,
        "abnormal": m.abnormal,
    }


def _serialize_report(r: HealthReport, metrics: list[ReportMetric] | None = None) -> dict:
    data = {
        "id": r.id,
        "file_name": r.file_name,
        "report_type": r.report_type,
        "hospital": r.hospital,
        "report_date": r.report_date.isoformat() if r.report_date else None,
        "status": r.status,
        "parse_engine": r.parse_engine,
        "summary": r.summary,
        "abnormal_count": r.abnormal_count,
        "metric_count": r.metric_count,
        "error_message": r.error_message,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "parsed_at": r.parsed_at.isoformat() if r.parsed_at else None,
    }
    if metrics is not None:
        data["metrics"] = [_serialize_metric(m) for m in metrics]
    return data


async def _parse_and_store(
    db: AsyncSession, report: HealthReport, content: bytes,
) -> None:
    """解析并落结构化指标；失败只记录脱敏原因，不抛给调用方。"""
    report.status = "parsing"
    await db.commit()
    try:
        parsed = await parse_report(content, report.file_name)
        metrics = parsed["metrics"]
        await db.execute(delete(ReportMetric).where(ReportMetric.report_id == report.id))
        for idx, item in enumerate(metrics):
            db.add(
                ReportMetric(
                    report_id=report.id,
                    patient_id=report.patient_id,
                    tenant_id=int(report.tenant_id or 1),
                    name=item["name"],
                    code=item["code"],
                    category=item["category"],
                    value_text=item["value_text"],
                    value_num=item["value_num"],
                    unit=item["unit"],
                    ref_low=item["ref_low"],
                    ref_high=item["ref_high"],
                    ref_text=item["ref_text"],
                    abnormal=item["abnormal"],
                    sort_order=idx,
                )
            )
        report.metric_count = len(metrics)
        report.abnormal_count = sum(1 for m in metrics if m["abnormal"] != "normal")
        report.summary = summarize_report(metrics, report.report_type)
        report.parse_engine = parsed["engine"]
        report.status = "parsed"
        report.parsed_at = datetime.now(timezone.utc)
        report.error_message = None
        if parsed.get("hospital") and not report.hospital:
            report.hospital = parsed["hospital"]
        if parsed.get("report_date") and not report.report_date:
            report.report_date = parsed["report_date"]
        await db.commit()
        _audit("parse", report.patient_id, report_id=report.id, metrics=len(metrics))
    except Exception as e:
        report.status = "failed"
        report.error_message = redact_text(f"{type(e).__name__}: {e}", 200)
        await db.commit()
        logger.warning(
            f"报告解析失败 report_id={report.id} patient_id={report.patient_id} "
            f"error={type(e).__name__}"
        )


@router.post("/upload")
async def upload_report(
    file: UploadFile = File(...),
    report_type: str = Form("lab"),
    auto_parse: bool = Form(True),
    ctx: AuthContext = Depends(require_auth()),
    db: AsyncSession = Depends(get_db),
):
    """上传报告（PDF/图片，≤20MB），默认立即解析。"""
    patient_id = _patient_id(ctx.subject_id)
    patient = (
        await db.execute(
            select(Patient).where(
                Patient.id == patient_id, Patient.tenant_id == ctx.tenant_id,
            )
        )
    ).scalar_one_or_none()
    if patient is None:
        raise BizException(BizErrorCode.NOT_FOUND, "患者档案不存在", status_code=404)

    filename = os.path.basename(file.filename or "")
    ext = os.path.splitext(filename)[1].lower()
    if ext not in ALLOWED_EXT:
        raise BizException(
            BizErrorCode.PARAM_ERROR,
            f"不支持的文件格式：{ext or '未知'}，支持 PDF/JPG/PNG/WEBP",
            status_code=400,
        )
    if report_type not in ALLOWED_REPORT_TYPES:
        report_type = "lab"

    content = await file.read()
    if not content:
        raise BizException(BizErrorCode.PARAM_ERROR, "文件内容为空", status_code=400)
    if len(content) > MAX_UPLOAD_BYTES:
        raise BizException(
            BizErrorCode.PARAM_ERROR,
            f"文件过大（上限 {MAX_UPLOAD_BYTES // 1024 // 1024}MB）",
            status_code=400,
        )

    object_key = f"patient-reports/{patient_id}/{uuid.uuid4().hex}{ext}"
    try:
        await aupload_file(object_key, content, file.content_type or "application/octet-stream")
    except Exception:
        logger.exception(f"报告上传 RustFS 失败 patient_id={patient_id}")
        raise BizException(BizErrorCode.INTERNAL, "文件存储暂时不可用，请稍后重试", status_code=500) from None

    report = HealthReport(
        patient_id=patient_id,
        tenant_id=ctx.tenant_id,
        file_name=filename[:255],
        object_key=object_key,
        file_size=len(content),
        mime_type=(file.content_type or "")[:100],
        report_type=report_type,
        status="uploaded",
    )
    db.add(report)
    await db.commit()
    await db.refresh(report)
    _audit("upload", patient_id, report_id=report.id, size=len(content))

    if auto_parse:
        await _parse_and_store(db, report, content)
        await db.refresh(report)

    metrics = (
        await db.execute(
            select(ReportMetric)
            .where(
                ReportMetric.report_id == report.id,
                ReportMetric.tenant_id == ctx.tenant_id,
            )
            .order_by(ReportMetric.sort_order.asc())
        )
    ).scalars().all()
    return {
        "message": "报告已上传" if report.status != "parsed" else "报告已解析",
        "report": _serialize_report(report, metrics),
    }


@router.get("")
async def list_reports(
    limit: int = 20,
    ctx: AuthContext = Depends(require_auth()),
    db: AsyncSession = Depends(get_db),
):
    patient_id = _patient_id(ctx.subject_id)
    rows = (
        await db.execute(
            select(HealthReport)
            .where(
                HealthReport.patient_id == patient_id,
                HealthReport.tenant_id == ctx.tenant_id,
                HealthReport.deleted_at.is_(None),
            )
            .order_by(HealthReport.created_at.desc())
            .limit(max(1, min(limit, 100)))
        )
    ).scalars().all()
    return {"reports": [_serialize_report(r) for r in rows], "total": len(rows)}


@router.get("/compare")
async def compare_metrics(
    code: str | None = None,
    name: str | None = None,
    limit: int = 12,
    ctx: AuthContext = Depends(require_auth()),
    db: AsyncSession = Depends(get_db),
):
    """跨报告指标对比：同一指标的历史取值序列。"""
    patient_id = _patient_id(ctx.subject_id)
    if not code and not name:
        raise BizException(BizErrorCode.PARAM_ERROR, "请指定要对比的指标", status_code=400)

    cond = ReportMetric.code == code if code else ReportMetric.name == name
    rows = (
        await db.execute(
            select(ReportMetric, HealthReport)
            .join(HealthReport, HealthReport.id == ReportMetric.report_id)
            .where(
                ReportMetric.patient_id == patient_id,
                ReportMetric.tenant_id == ctx.tenant_id,
                cond,
                HealthReport.deleted_at.is_(None),
                HealthReport.tenant_id == ctx.tenant_id,
            )
            .order_by(HealthReport.created_at.asc())
            .limit(max(2, min(limit, 50)))
        )
    ).all()

    points = [
        {
            "report_id": report.id,
            "report_date": (
                report.report_date.isoformat() if report.report_date
                else (report.created_at.date().isoformat() if report.created_at else None)
            ),
            "value_num": metric.value_num,
            "value_text": metric.value_text,
            "unit": metric.unit,
            "ref_low": metric.ref_low,
            "ref_high": metric.ref_high,
            "abnormal": metric.abnormal,
        }
        for metric, report in rows
    ]
    numeric = [p["value_num"] for p in points if p["value_num"] is not None]
    delta = round(numeric[-1] - numeric[0], 2) if len(numeric) >= 2 else None
    return {
        "name": rows[0][0].name if rows else (name or code),
        "code": code,
        "unit": points[0]["unit"] if points else "",
        "ref_low": points[0]["ref_low"] if points else None,
        "ref_high": points[0]["ref_high"] if points else None,
        "points": points,
        "delta": delta,
        "direction": "up" if (delta or 0) > 0 else ("down" if (delta or 0) < 0 else "flat"),
        "total": len(points),
    }


@router.get("/{report_id}")
async def get_report(
    report_id: int,
    ctx: AuthContext = Depends(require_auth()),
    db: AsyncSession = Depends(get_db),
):
    patient_id = _patient_id(ctx.subject_id)
    report = (
        await db.execute(
            select(HealthReport).where(
                HealthReport.id == report_id,
                HealthReport.patient_id == patient_id,
                HealthReport.tenant_id == ctx.tenant_id,
                HealthReport.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if report is None:
        raise BizException(BizErrorCode.NOT_FOUND, "报告不存在", status_code=404)
    metrics = (
        await db.execute(
            select(ReportMetric)
            .where(
                ReportMetric.report_id == report.id,
                ReportMetric.tenant_id == ctx.tenant_id,
            )
            .order_by(ReportMetric.sort_order.asc())
        )
    ).scalars().all()
    _audit("detail", patient_id, report_id=report_id)
    return {"report": _serialize_report(report, metrics)}


@router.post("/{report_id}/parse")
async def reparse_report(
    report_id: int,
    ctx: AuthContext = Depends(require_auth()),
    db: AsyncSession = Depends(get_db),
):
    patient_id = _patient_id(ctx.subject_id)
    report = (
        await db.execute(
            select(HealthReport).where(
                HealthReport.id == report_id,
                HealthReport.patient_id == patient_id,
                HealthReport.tenant_id == ctx.tenant_id,
                HealthReport.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if report is None:
        raise BizException(BizErrorCode.NOT_FOUND, "报告不存在", status_code=404)

    try:
        content = await to_thread(download_file, report.object_key)
    except Exception:
        logger.exception(f"报告原文读取失败 report_id={report_id}")
        raise BizException(BizErrorCode.INTERNAL, "报告原文暂时不可用", status_code=500) from None

    await _parse_and_store(db, report, content)
    await db.refresh(report)
    metrics = (
        await db.execute(
            select(ReportMetric)
            .where(
                ReportMetric.report_id == report.id,
                ReportMetric.tenant_id == ctx.tenant_id,
            )
            .order_by(ReportMetric.sort_order.asc())
        )
    ).scalars().all()
    return {"message": "解析完成" if report.status == "parsed" else "解析失败，请重试", "report": _serialize_report(report, metrics)}


@router.delete("/{report_id}")
async def delete_report(
    report_id: int,
    ctx: AuthContext = Depends(require_auth()),
    db: AsyncSession = Depends(get_db),
):
    patient_id = _patient_id(ctx.subject_id)
    report = (
        await db.execute(
            select(HealthReport).where(
                HealthReport.id == report_id,
                HealthReport.patient_id == patient_id,
                HealthReport.tenant_id == ctx.tenant_id,
            )
        )
    ).scalar_one_or_none()
    if report is None:
        raise BizException(BizErrorCode.NOT_FOUND, "报告不存在", status_code=404)

    report.deleted_at = datetime.now(timezone.utc)
    await db.commit()
    try:
        await to_thread(delete_object, report.object_key)
    except Exception as e:
        logger.warning(f"报告对象删除失败（保留软删除记录）: {e}")
    _audit("delete", patient_id, report_id=report_id)
    return {"message": "报告已删除"}
