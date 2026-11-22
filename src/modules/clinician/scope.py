# src/modules/clinician/scope.py
#
# 医护可见范围（C4）唯一实现。接口层只调用这里的函数，不自己拼判定，
# 否则三个接口会长出三套不一致的越权规则。
#
# 规则（与 model.py 的设计说明一致）：
#   1. 责任患者：全等级可读（他的急症记录与对话都属于诊疗必需）；
#   2. 非责任患者：仅当"患者最近就诊科室"∈医生绑定科室，且事件等级为 red/orange；
#   3. 其余不可读。
#
# 第 2 条刻意只放行高危两档：科室级告警的意义是"本科室有危重患者需要立刻处置"，
# 把 yellow 也广播给全科室医生没有临床价值，只是扩大可见面。

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.clinical.model import LEVEL_ORANGE, LEVEL_RED
from src.modules.clinician.model import (
    RELATION_ASSIGNED,
    ClinicianDepartment,
    ClinicianPatient,
)

# 允许跨患者广播到科室的等级：只有高危两档
ALERT_LEVELS = (LEVEL_RED, LEVEL_ORANGE)

# 可见原因（接口回给前端，便于医生理解"为什么这条在我这儿"）
REASON_RESPONSIBLE = "responsible"      # 责任患者
REASON_DEPARTMENT = "department_alert"  # 本科室高危告警
REASON_DENIED = "denied"


@dataclass(frozen=True)
class ClinicianScope:
    """一个医生账号的可见范围快照（一次请求内复用，避免 N+1）。"""

    clinician_id: int
    department_names: frozenset[str]
    patient_ids: frozenset[int]

    @property
    def has_department(self) -> bool:
        return bool(self.department_names)

    def responsible(self, patient_id: int | None) -> bool:
        return patient_id is not None and patient_id in self.patient_ids


async def build_scope(db: AsyncSession, clinician_id: int) -> ClinicianScope:
    """读取该医生的科室绑定与责任患者。"""
    dept_rows = (
        await db.execute(
            select(ClinicianDepartment).where(
                ClinicianDepartment.clinician_id == clinician_id
            )
        )
    ).scalars().all()
    patient_rows = (
        await db.execute(
            select(ClinicianPatient).where(
                ClinicianPatient.clinician_id == clinician_id
            )
        )
    ).scalars().all()

    names: set[str] = set()
    if dept_rows:
        from src.modules.medical.model import Department

        dept_ids = [d.department_id for d in dept_rows]
        rows = (
            await db.execute(
                select(Department.name).where(Department.id.in_(dept_ids))
            )
        ).scalars().all()
        names = {str(n) for n in rows if n}

    return ClinicianScope(
        clinician_id=clinician_id,
        department_names=frozenset(names),
        patient_ids=frozenset(p.patient_id for p in patient_rows),
    )


async def recent_patient_departments(
    db: AsyncSession, patient_ids: list[int], *, limit: int = 2,
) -> dict[int, set[str]]:
    """批量取每个患者最近的就诊科室名（来源：appointments）。

    只看最近 limit 条：患者在多个科室看过病时，把历史所有科室都算作"可见科室"
    会让授权面随时间无限扩大，这不是临床需要。
    """
    from src.modules.medical.model import Appointment

    out: dict[int, set[str]] = {pid: set() for pid in patient_ids}
    if not patient_ids:
        return out

    stmt = (
        select(Appointment.patient_id, Appointment.department)
        .where(Appointment.patient_id.in_(patient_ids))
        .order_by(Appointment.patient_id, Appointment.date.desc(), Appointment.id.desc())
    )
    for pid, dept in (await db.execute(stmt)).all():
        names = out.setdefault(pid, set())
        if dept and len(names) < limit:
            names.add(str(dept))
    return out


def can_read_event(
    scope: ClinicianScope,
    *,
    patient_id: int | None,
    level: str,
    patient_departments: set[str] | None = None,
) -> tuple[bool, str]:
    """返回 (是否可见, 原因)。原因同时用于审计日志。"""
    # 没有 patient_id 的事件（游客或未识别身份）不进任何医护的可见范围：
    # 归属不明的数据不该因为"科室匹配"就被医生看到。
    if patient_id is None:
        return False, REASON_DENIED
    if scope.responsible(patient_id):
        return True, REASON_RESPONSIBLE
    if level in ALERT_LEVELS and patient_departments:
        if scope.department_names & patient_departments:
            return True, REASON_DEPARTMENT
    return False, REASON_DENIED


async def link_patient(
    db: AsyncSession,
    *,
    clinician_id: int,
    patient_id: int,
    relation: str = RELATION_ASSIGNED,
    department_id: int | None = None,
    granted_by: str = "",
    commit: bool = True,
) -> ClinicianPatient:
    """建立责任关系（幂等）：已存在则更新来源备注，不重复插入。"""
    existing = (
        await db.execute(
            select(ClinicianPatient).where(
                ClinicianPatient.clinician_id == clinician_id,
                ClinicianPatient.patient_id == patient_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if relation and existing.relation != relation:
            existing.relation = relation
        if department_id is not None:
            existing.department_id = department_id
        if granted_by:
            existing.granted_by = granted_by
        if commit:
            await db.commit()
            await db.refresh(existing)
        return existing

    row = ClinicianPatient(
        clinician_id=clinician_id,
        patient_id=patient_id,
        relation=relation,
        department_id=department_id,
        granted_by=granted_by,
    )
    db.add(row)
    if commit:
        await db.commit()
        await db.refresh(row)
    return row


async def link_department(
    db: AsyncSession,
    *,
    clinician_id: int,
    department_id: int,
    is_primary: bool = False,
    granted_by: str = "",
    commit: bool = True,
) -> ClinicianDepartment:
    """绑定科室（幂等）。"""
    existing = (
        await db.execute(
            select(ClinicianDepartment).where(
                ClinicianDepartment.clinician_id == clinician_id,
                ClinicianDepartment.department_id == department_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.is_primary = is_primary or existing.is_primary
        if granted_by:
            existing.granted_by = granted_by
        if commit:
            await db.commit()
            await db.refresh(existing)
        return existing

    row = ClinicianDepartment(
        clinician_id=clinician_id,
        department_id=department_id,
        is_primary=is_primary,
        granted_by=granted_by,
    )
    db.add(row)
    if commit:
        await db.commit()
        await db.refresh(row)
    return row
