# src/modules/clinical/decide.py
#
# 决策入口的统一收口（C4）：
# 患者自助确认与医护代批走同一套"查动作 → 执行 → 清指针 → 幂等回查"，
# 避免两条路由各自实现一遍导致结果口径分叉（B2 时回查逻辑只写在患者端路由里）。
#
# 接诊后自动建责任关系（C4 续）也挂在这里：这是"预约真正生成"的唯一收口，
# 患者自助确认与医护代批都会经过它；若挂在各路由上，两条路径迟早漏一条。

from __future__ import annotations

from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.clinical import actions as clinical
from src.modules.clinical.executors import executor_for
from src.modules.clinical.link import forget_pending
from src.modules.clinical.model import ClinicalAction


async def _lookup_result(db: AsyncSession, action: ClinicalAction) -> dict[str, Any]:
    """幂等重放时按 result_ref 回查预约，保证客户端重试也拿得到预约号。"""
    if not action.result_ref:
        return {}
    appt = await _appointment_by_no(db, action.result_ref)
    if appt is None:
        return {}
    return _serialize_appointment(appt)


def _serialize_appointment(appt: Any) -> dict[str, Any]:
    return {
        "appointment_no": appt.appointment_no,
        "appointment_id": appt.id,
        "department": appt.department,
        "doctor_name": appt.doctor_name,
        "doctor_clinician_id": appt.doctor_clinician_id,
        "date": appt.date,
        "time_slot": appt.time_slot,
        "fee": appt.fee,
        "queue_number": appt.queue_number,
        "status": appt.status,
    }


async def _appointment_by_no(db: AsyncSession, appointment_no: str) -> Any:
    from src.modules.medical.model import Appointment

    return (
        await db.execute(
            select(Appointment).where(Appointment.appointment_no == appointment_no)
        )
    ).scalar_one_or_none()


async def _load_appointment(
    db: AsyncSession, action: ClinicalAction, data: dict[str, Any],
) -> Any:
    """从执行结果或 result_ref 取回刚建的预约（两条路径口径不同，这里统一）。"""
    from src.modules.medical.model import Appointment

    appt_id = data.get("appointment_id") if data else None
    if appt_id:
        appt = await db.get(Appointment, int(appt_id))
        if appt is not None:
            return appt
    if action.result_ref:
        return await _appointment_by_no(db, action.result_ref)
    appt_no = data.get("appointment_no") if data else None
    if appt_no:
        return await _appointment_by_no(db, str(appt_no))
    return None


async def _touch_consulted(
    db: AsyncSession, action: ClinicalAction, data: dict[str, Any],
) -> None:
    """接诊确认后把"医生 ↔ 患者"关系补成 consulted。

    只在预约有 doctor_clinician_id 时动手：没有医生账号就不存在"谁的接诊"，
    硬按科室猜一个医生去授权，就是越权。
    """
    from src.modules.clinician.profile import link_consulted

    appt = await _load_appointment(db, action, data)
    if appt is None:
        return
    doctor_id = appt.doctor_clinician_id
    patient_id = appt.patient_id
    if not doctor_id or not patient_id:
        return
    if int(doctor_id) == int(patient_id):
        # 医护账号自己下单不该把自己加成"责任患者"
        return

    row = await link_consulted(
        db,
        clinician_id=int(doctor_id),
        patient_id=int(patient_id),
        department_name=str(appt.department or ""),
        granted_by=f"consulted:action:{action.id}",
    )
    if row is not None:
        logger.info(
            f"接诊建关系：action={action.id} clinician={doctor_id} "
            f"patient={patient_id} relation={row.relation} appt={appt.appointment_no}"
        )


async def approve_action(
    db: AsyncSession,
    action_id: int,
    *,
    actor: str,
    note: str = "",
) -> tuple[ClinicalAction, dict[str, Any]]:
    action = await clinical.get_action(db, action_id)
    executed, data = await clinical.approve(
        db,
        action_id,
        actor=actor,
        executor=executor_for(action.action_type),
        note=note,
    )
    await forget_pending(executed.thread_id)
    if not data and executed.result_ref:
        data = await _lookup_result(db, executed)
        if data:
            logger.info(f"动作 {action_id} 幂等重放，按 ref={executed.result_ref} 回查结果")
    if action.action_type in ("appointment", "handoff"):
        await _touch_consulted(db, executed, data)
    return executed, data


async def reject_action(
    db: AsyncSession,
    action_id: int,
    *,
    actor: str,
    reason: str = "",
) -> ClinicalAction:
    rejected = await clinical.reject(db, action_id, actor=actor, reason=reason)
    await forget_pending(rejected.thread_id)
    return rejected
