# src/api/routers/appointment.py
#
# 挂号预约 API：
# - POST /            ：直接创建挂号（前端确认后调用）
# - GET  /            ：查询本人预约列表
# - GET  /{id}        ：预约详情
# - PATCH /{id}/cancel：取消预约
#
# 身份一律取 JWT（患者 ID），**不接受请求体/查询参数自报 user_id**：
# 此前该 router 完全无鉴权，任何人都能按 ID 读写他人预约并读到诊断字段。
#
# 问诊流程内的自动挂号不走这里，由 handle_handoff 落库（见 inquiry_agent.py）。

from __future__ import annotations
import uuid

from fastapi import APIRouter, Depends

from src.core.exceptions import BizErrorCode, BizException
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_current_user_id
from src.infra.database import get_db
from src.modules.medical.model import Appointment

# router 级守卫：即使以后新增端点忘记写 Depends，也不会裸奔
router = APIRouter(
    prefix="/api/v1/appointments",
    tags=["appointments"],
    dependencies=[Depends(get_current_user_id)],
)


def _patient_id(current_user: str) -> int:
    """token 的 sub 即患者档案 ID；非数字（如游客/管理员）一律拒绝。"""
    try:
        return int(current_user)
    except (TypeError, ValueError):
        raise BizException(
            BizErrorCode.UNAUTHORIZED, "登录信息无效，请重新登录", status_code=401,
        ) from None


def _serialize(a: Appointment, *, include_diagnosis: bool = True) -> dict:
    data = {
        "id": a.id,
        "patient_id": a.patient_id,
        "session_id": a.session_id,
        "appointment_no": a.appointment_no,
        "doctor_name": a.doctor_name,
        "department": a.department,
        "date": a.date,
        "time_slot": a.time_slot,
        "fee": a.fee,
        "queue_number": a.queue_number,
        "status": a.status,
        "created_at": str(a.created_at) if a.created_at else None,
    }
    if include_diagnosis:
        data["diagnosis"] = a.diagnosis
    return data


class AppointmentCreate(BaseModel):
    """创建预约的入参：不含任何身份字段，患者身份只从 token 取。"""

    session_id: str = ""
    department: str
    doctor_name: str = ""
    date: str = ""
    time_slot: str = ""
    fee: float = 0
    queue_number: int = 0
    diagnosis: str = ""


@router.post("")
async def create_appointment(
    req: AppointmentCreate,
    current_user: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """直接创建一条挂号预约（前端确认 Modal 调用）。"""
    patient_id = _patient_id(current_user)

    appt = Appointment(
        patient_id=patient_id,
        session_id=req.session_id or None,
        appointment_no=f"APT{uuid.uuid4().hex[:8].upper()}",
        doctor_name=req.doctor_name,
        department=req.department,
        date=req.date,
        time_slot=req.time_slot,
        fee=req.fee,
        queue_number=req.queue_number,
        status="confirmed",
        diagnosis=req.diagnosis or None,
    )
    db.add(appt)
    await db.flush()
    return {"message": "预约成功", "appointment": _serialize(appt)}


@router.get("")
async def list_appointments(
    start_date: str | None = None,
    end_date: str | None = None,
    status: str | None = None,
    limit: int = 100,
    current_user: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """查询本人预约列表，按日期倒序。"""
    patient_id = _patient_id(current_user)
    # limit 上限收敛：避免一次拉全表
    limit = max(1, min(limit, 200))

    q = select(Appointment).where(Appointment.patient_id == patient_id)
    if start_date:
        q = q.where(Appointment.date >= start_date)
    if end_date:
        q = q.where(Appointment.date <= end_date)
    if status:
        q = q.where(Appointment.status == status)

    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar() or 0
    rows = (await db.execute(
        q.order_by(Appointment.date.desc(), Appointment.id.desc()).limit(limit)
    )).scalars().all()
    return {"appointments": [_serialize(a) for a in rows], "total": total}


async def _load_own(db: AsyncSession, appointment_id: int, patient_id: int) -> Appointment:
    """按 (id, patient_id) 取预约：他人预约返回 404 而不是 403，避免探测 ID 是否存在。"""
    appt = await db.get(Appointment, appointment_id)
    if appt is None or appt.patient_id != patient_id:
        raise BizException(BizErrorCode.NOT_FOUND, "预约不存在", status_code=404)
    return appt


@router.get("/{appointment_id}")
async def get_appointment(
    appointment_id: int,
    current_user: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    appt = await _load_own(db, appointment_id, _patient_id(current_user))
    return {"appointment": _serialize(appt)}


@router.patch("/{appointment_id}/cancel")
async def cancel_appointment(
    appointment_id: int,
    current_user: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """取消预约（软删除：status -> cancelled）。"""
    appt = await _load_own(db, appointment_id, _patient_id(current_user))
    if appt.status == "cancelled":
        return {"message": "预约已取消", "appointment": _serialize(appt)}
    appt.status = "cancelled"
    await db.flush()
    return {"message": "预约已取消", "appointment": _serialize(appt)}
