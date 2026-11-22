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

from src.api.deps import AuthContext, get_current_user_id, require_auth, require_scope
from src.infra.database import get_db
from src.modules.medical.model import Appointment

# router 级守卫：即使以后新增端点忘记写 Depends，也不会裸奔
router = APIRouter(
    prefix="/api/v1/appointments",
    tags=["appointments"],
    dependencies=[Depends(get_current_user_id), Depends(require_scope("appointment:read"))],
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
        "doctor_clinician_id": a.doctor_clinician_id,
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
    # 前端从 /doctors 目录里选中的医生账号；不传则后端按(科室, 姓名)唯一匹配补
    doctor_clinician_id: int = 0
    date: str = ""
    time_slot: str = ""
    fee: float = 0
    queue_number: int = 0
    diagnosis: str = ""


@router.post("")
async def create_appointment(
    req: AppointmentCreate,
    ctx: AuthContext = Depends(require_auth()),
    db: AsyncSession = Depends(get_db),
):
    """直接创建一条挂号预约（前端确认 Modal 调用）。

    B2 收口：不再直接插 confirmed 行，而是「建草稿 → 立即批准」。
    手动下单与对话下单走同一条状态机，落库、审计、幂等口径一致；
    也因此这条路径不会再出现"绕过确认门控直接写死预约"的情况。
    """
    from src.modules.clinical import actions as clinical
    from src.modules.clinical.decide import approve_action
    from src.modules.clinical.model import ACTION_APPOINTMENT, SOURCE_CLINICIAN

    patient_id = _patient_id(ctx.subject_id)
    current_user = ctx.subject_id

    row = await clinical.create_action(
        db,
        action_type=ACTION_APPOINTMENT,
        payload={
            "department": req.department,
            "doctor_name": req.doctor_name,
            "doctor_clinician_id": req.doctor_clinician_id or None,
            "date": req.date,
            "time_slot": req.time_slot,
            "fee": req.fee,
            "queue_number": req.queue_number,
            "diagnosis": req.diagnosis,
        },
        summary=f"{req.department}｜{req.date} {req.time_slot}".strip("｜ "),
        patient_id=patient_id,
        user_id=current_user,
        session_id=req.session_id,
        thread_id=f"{current_user}:{req.session_id}",
        source=SOURCE_CLINICIAN,
        requested_by="api:appointment_create",
        tenant_id=ctx.tenant_id,
    )
    # 走 decide 统一收口（而不是直接 clinical.approve）：接诊建关系等后置动作只实现一处
    _executed, data = await approve_action(
        db, row.id, actor=f"patient:{current_user}",
    )

    appt = await db.get(Appointment, data["appointment_id"])
    return {"message": "预约成功", "appointment": _serialize(appt)}


@router.get("")
async def list_appointments(
    start_date: str | None = None,
    end_date: str | None = None,
    status: str | None = None,
    limit: int = 100,
    ctx: AuthContext = Depends(require_auth()),
    db: AsyncSession = Depends(get_db),
):
    """查询本人预约列表，按日期倒序。"""
    patient_id = _patient_id(ctx.subject_id)
    # limit 上限收敛：避免一次拉全表
    limit = max(1, min(limit, 200))

    q = select(Appointment).where(
        Appointment.patient_id == patient_id,
        Appointment.tenant_id == ctx.tenant_id,
    )
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


@router.get("/doctors")
async def list_doctors(
    department_id: int | None = None,
    ctx: AuthContext = Depends(require_auth()),
    db: AsyncSession = Depends(get_db),
):
    """可挂号医生目录（患者端选医生用）。

    与 /api/v1/admin/clinicians/directory 共用同一个取数函数：
    两边各写一份"谁在岗"，迟早出现管理端看得到、患者端选不到。
    不返回工号与手机号 —— 挂号不需要这些。
    """
    from src.modules.clinician.profile import list_directory

    items = await list_directory(db, department_id=department_id, tenant_id=ctx.tenant_id)
    return {
        "doctors": [
            {
                "clinician_id": d["clinician_id"],
                "name": d["name"],
                "title": d["title"],
                "specialty": d["specialty"],
                "intro": d["intro"],
                "departments": d["departments"],
            }
            for d in items
        ],
        "total": len(items),
    }


async def _load_own(
    db: AsyncSession, appointment_id: int, patient_id: int, tenant_id: int,
) -> Appointment:
    """按 (id, patient_id, tenant_id) 取预约：他人或他租户预约返回 404，避免探测 ID 是否存在。"""
    appt = await db.get(Appointment, appointment_id)
    if (
        appt is None
        or appt.patient_id != patient_id
        or int(appt.tenant_id or 1) != int(tenant_id)
    ):
        raise BizException(BizErrorCode.NOT_FOUND, "预约不存在", status_code=404)
    return appt


@router.get("/{appointment_id}")
async def get_appointment(
    appointment_id: int,
    ctx: AuthContext = Depends(require_auth()),
    db: AsyncSession = Depends(get_db),
):
    appt = await _load_own(db, appointment_id, _patient_id(ctx.subject_id), ctx.tenant_id)
    return {"appointment": _serialize(appt)}


@router.patch("/{appointment_id}/cancel")
async def cancel_appointment(
    appointment_id: int,
    ctx: AuthContext = Depends(require_auth()),
    db: AsyncSession = Depends(get_db),
):
    """取消预约（软删除：status -> cancelled）。"""
    appt = await _load_own(db, appointment_id, _patient_id(ctx.subject_id), ctx.tenant_id)
    if appt.status == "cancelled":
        return {"message": "预约已取消", "appointment": _serialize(appt)}
    appt.status = "cancelled"
    await db.flush()
    return {"message": "预约已取消", "appointment": _serialize(appt)}
