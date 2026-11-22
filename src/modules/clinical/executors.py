# src/modules/clinical/executors.py
#
# 动作执行器：状态机批准后真正落库的那一步。
#
# 为什么单独一层：
#   actions.py 只管状态流转，不认识 appointments 表；具体"批准后干什么"放在这里，
#   由 API / chat 层在 approve 时传进来。这样状态机可以被后续的用药建议、检查开单复用。
#
# 两条执行路径：
#   - execute_appointment：工具层（guide_tools）生成的挂号草稿，参数在草稿创建时就齐了，
#     批准后直接写一条 confirmed 预约，不再等待模型
#   - execute_handoff     ：问诊链路生成的移交草稿，医生/时段需要模型现场生成，
#     批准后才调用（把 20 秒的 LLM 耗时从"每轮对话"挪到"用户点确认之后"）
#
# 约定：执行器在传入的 db 会话上操作，不自己 commit —— 由 actions.approve 统一提交，
# 执行失败时整个事务回滚，状态退回 pending_confirm。

from __future__ import annotations

import uuid
from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.clinical.model import ClinicalAction


def _new_appointment_no() -> str:
    return f"APT{uuid.uuid4().hex[:8].upper()}"


def _as_int(value: Any) -> int | None:
    """payload 里的 doctor_clinician_id 可能来自 JSON（int/str），非法值一律当没给。"""
    if value is None or value == "":
        return None
    try:
        out = int(value)
    except (TypeError, ValueError):
        return None
    return out if out > 0 else None


async def execute_appointment(db: AsyncSession, action: ClinicalAction) -> tuple[str, dict[str, Any]]:
    """按草稿参数直接建一条预约（患者已明确确认）。"""
    from src.modules.clinician.profile import ensure_doctor, resolve_doctor, sole_doctor
    from src.modules.medical.model import Appointment

    p = action.payload or {}
    appointment_no = str(p.get("appointment_no") or _new_appointment_no())
    department = str(p.get("department") or "")
    doctor_name = str(p.get("doctor_name") or "待分配")

    # 医生账号优先取草稿里显式给的（患者选了具体医生），否则按(科室, 姓名)唯一匹配补；
    # 补不上就留空 —— 预约照常建，只是不会自动建立 consulted 责任关系。
    doctor_clinician_id = await ensure_doctor(db, _as_int(p.get("doctor_clinician_id")))
    if doctor_clinician_id is None:
        doctor_clinician_id = await resolve_doctor(
            db, department=department, doctor_name=doctor_name,
        )
    if doctor_clinician_id is None:
        # 科室只有一位在岗医生时不存在歧义，直接落到他名下（并把卡片上的医生名改对）；
        # 多于一位就不猜，宁可这条预约不建关系。
        sole = await sole_doctor(db, department=department)
        if sole is not None:
            doctor_clinician_id = sole["clinician_id"]
            doctor_name = f"{sole['name']} {sole['title']}".strip()

    appt = Appointment(
        patient_id=action.patient_id,
        session_id=action.session_id or None,
        appointment_no=appointment_no,
        doctor_name=doctor_name,
        doctor_clinician_id=doctor_clinician_id,
        department=department,
        date=str(p.get("date") or ""),
        time_slot=str(p.get("time_slot") or "待分配"),
        fee=float(p.get("fee") or 0),
        queue_number=int(p.get("queue_number") or 0),
        status="confirmed",
        diagnosis=(str(p["diagnosis"])[:200] if p.get("diagnosis") else None),
    )
    db.add(appt)
    await db.flush()

    data = {
        "appointment_no": appointment_no,
        "appointment_id": appt.id,
        "department": appt.department,
        "doctor_name": appt.doctor_name,
        "doctor_clinician_id": appt.doctor_clinician_id,
        "date": appt.date,
        "time_slot": appt.time_slot,
        "fee": appt.fee,
        "queue_number": appt.queue_number,
        "status": "confirmed",
    }
    logger.info(
        f"确认后建预约成功 no={appointment_no} patient={action.patient_id} "
        f"doctor_clinician={appt.doctor_clinician_id}"
    )
    return appointment_no, data


async def execute_handoff(db: AsyncSession, action: ClinicalAction) -> tuple[str, dict[str, Any]]:
    """问诊移交草稿：此刻才生成医生/时段并落库。

    payload 里存的是 InquiryHandoffPayload 的 dict（见 worker_tools），
    所以这里可以完整还原患者上下文与结论。
    """
    from src.agents.inquiry.state import InquiryHandoffPayload
    from src.agents.workers.inquiry_agent import mock_create_appointment
    from src.modules.clinician.profile import ensure_doctor, resolve_doctor, sole_doctor
    from src.modules.medical.model import Appointment

    p = action.payload or {}
    try:
        payload = InquiryHandoffPayload.model_validate(p.get("handoff_payload") or p)
    except Exception as e:  # noqa: BLE001 —— 历史草稿结构异常时退回最小可用字段
        logger.warning(f"移交草稿结构解析失败，使用最小字段: {type(e).__name__}: {e}")
        payload = InquiryHandoffPayload(
            patient_id=action.patient_id,
            department=str(p.get("department") or ""),
            session_id=action.session_id,
        )

    if action.patient_id and not payload.patient_id:
        payload.patient_id = action.patient_id
    if action.session_id and not payload.session_id:
        payload.session_id = action.session_id

    result = await mock_create_appointment(payload)
    appointment_no = _new_appointment_no()

    # 移交链路里医生是模型现场生成的（文本姓名），所以这里按科室+姓名唯一匹配补账号；
    # 匹配不上就留空，与直接挂号保持同一口径。
    doctor_clinician_id = await ensure_doctor(db, _as_int(p.get("doctor_clinician_id")))
    if doctor_clinician_id is None:
        doctor_clinician_id = await resolve_doctor(
            db, department=result.department, doctor_name=result.doctor_name,
        )
    if doctor_clinician_id is None:
        # 科室唯一在岗医生：把模型生成的虚拟医生名换成真实医生，避免卡片显示一个查无此人的名字
        sole = await sole_doctor(db, department=result.department)
        if sole is not None:
            doctor_clinician_id = sole["clinician_id"]
            result.doctor_name = f"{sole['name']} {sole['title']}".strip()

    appt = Appointment(
        patient_id=action.patient_id,
        session_id=action.session_id or None,
        appointment_no=appointment_no,
        doctor_name=result.doctor_name,
        doctor_clinician_id=doctor_clinician_id,
        department=result.department,
        date=result.date,
        time_slot=result.time_slot,
        fee=result.fee,
        queue_number=result.queue_number,
        status="confirmed",
        diagnosis=payload.primary_disease or None,
    )
    db.add(appt)
    await db.flush()

    data = {
        "appointment_no": appointment_no,
        "appointment_id": appt.id,
        "department": appt.department,
        "doctor_name": appt.doctor_name,
        "doctor_clinician_id": appt.doctor_clinician_id,
        "date": appt.date,
        "time_slot": appt.time_slot,
        "fee": appt.fee,
        "queue_number": appt.queue_number,
        "status": "confirmed",
    }
    logger.info(
        f"确认后问卷移交建预约成功 no={appointment_no} patient={action.patient_id} "
        f"doctor_clinician={appt.doctor_clinician_id}"
    )
    return appointment_no, data


# 动作类型 -> 执行器
EXECUTORS = {
    "appointment": execute_appointment,
    "handoff": execute_handoff,
}


def executor_for(action_type: str):
    fn = EXECUTORS.get(str(action_type or ""))
    if fn is None:
        raise ValueError(f"动作类型 {action_type!r} 没有注册执行器")
    return fn
