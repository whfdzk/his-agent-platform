# src/modules/clinician/profile.py
#
# 医生档案与"接诊后自动建立责任关系"（C4 续）。
#
# 要解决的问题：
#   appointments.doctor_name 是自由文本（"张伟 主任医师"），
#   clinician_patients 却需要 clinician_id 才能回答"这个医生能看谁"。
#   两者之间缺一层可映射的档案 —— 没有它，接诊后医生看不到自己刚接的患者，
#   急症看板与我的患者页都只能靠管理员手工绑定。
#
# 两条约束（这也是为什么不能"按名字猜"就完事）：
#   1. 映射必须以 doctor_clinician_id 为准；姓名匹配只用于把草稿里的文本补成账号，
#      且必须"唯一命中"，命中多个宁可不绑 —— 绑错人等于把患者记录授权给别的医生。
#   2. 自动建立只升不降：已有的 assigned（管理员主动分配）不能被 consulted 覆盖。
#
# 档案本身不参与授权判定，scope.py 不读这张表；它只提供"谁能被挂号、展示什么"。

from __future__ import annotations

import re
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.clinician.model import (
    PROFILE_ACTIVE,
    RELATION_ASSIGNED,
    RELATION_CONSULTED,
    ClinicianDepartment,
    ClinicianPatient,
    ClinicianProfile,
)

# 医生姓名里常见的职称后缀：匹配时先剥掉，避免"张伟 主任医师"对不上"张伟"
_TITLE_WORDS = (
    "副主任医师", "主任医师", "主治医师", "住院医师",
    "副教授", "教授", "医师", "医生", "护师", "护士",
)
_SPACES = re.compile(r"\s+")


def normalize_doctor_name(text: Any) -> str:
    """把医生姓名归一成可比较的字符串：去空白、去职称后缀。"""
    s = _SPACES.sub("", str(text or ""))
    for word in _TITLE_WORDS:
        s = s.replace(word, "")
    return s


async def get_profile(db: AsyncSession, clinician_id: int) -> ClinicianProfile | None:
    return (
        await db.execute(
            select(ClinicianProfile).where(ClinicianProfile.clinician_id == clinician_id)
        )
    ).scalar_one_or_none()


_PROFILE_FIELDS = ("employee_no", "title", "specialty", "intro", "status", "note")


async def upsert_profile(
    db: AsyncSession,
    *,
    clinician_id: int,
    fields: dict[str, Any],
    commit: bool = True,
) -> ClinicianProfile:
    """写入/更新医生档案（幂等）。只覆盖显式传入的字段，未传的保持原值。"""
    row = await get_profile(db, clinician_id)
    if row is None:
        row = ClinicianProfile(clinician_id=clinician_id, **{
            k: v for k, v in fields.items() if k in _PROFILE_FIELDS and v is not None
        })
        db.add(row)
    else:
        for k in _PROFILE_FIELDS:
            if k in fields and fields[k] is not None:
                setattr(row, k, fields[k])
    if commit:
        await db.commit()
        await db.refresh(row)
    return row


async def department_id_of(db: AsyncSession, department_name: str) -> int | None:
    """科室名 → departments.id（精确匹配）。业务表科室存的是名称字符串。"""
    name = str(department_name or "").strip()
    if not name:
        return None
    from src.modules.medical.model import Department

    return (
        await db.execute(select(Department.id).where(Department.name == name))
    ).scalar_one_or_none()


async def list_directory(
    db: AsyncSession,
    *,
    department_id: int | None = None,
    include_inactive: bool = False,
    tenant_id: int | None = None,
) -> list[dict[str, Any]]:
    """医生目录：可被挂号的医生（有档案 + 在岗 + 仍有 clinician 角色）。

    为什么目录要卡角色：档案是数据，角色才是权限来源。角色被回收后档案还在，
    若目录仍列出该医生，患者会挂到一个已经无权查看自己的人身上。
    tenant_id 非空时，档案与科室绑定都限定在本租户内。
    """
    from src.core.permissions import KIND_PATIENT
    from src.modules.medical.model import Department, Patient
    from src.modules.rbac.model import RbacSubjectRole

    stmt = (
        select(ClinicianProfile, Patient.name)
        .join(Patient, Patient.id == ClinicianProfile.clinician_id)
        .join(
            RbacSubjectRole,
            (RbacSubjectRole.subject_id == ClinicianProfile.clinician_id)
            & (RbacSubjectRole.subject_kind == KIND_PATIENT)
            & (RbacSubjectRole.role_code == "clinician"),
        )
        .where(Patient.enabled.is_(True))
    )
    if tenant_id is not None:
        stmt = stmt.where(
            ClinicianProfile.tenant_id == tenant_id, Patient.tenant_id == tenant_id,
        )
    if not include_inactive:
        stmt = stmt.where(ClinicianProfile.status == PROFILE_ACTIVE)

    rows = (await db.execute(stmt.order_by(ClinicianProfile.clinician_id))).all()
    items: dict[int, dict[str, Any]] = {}
    for profile, name in rows:
        items[int(profile.clinician_id)] = {
            "clinician_id": int(profile.clinician_id),
            "name": str(name or ""),
            "employee_no": str(profile.employee_no or ""),
            "title": str(profile.title or ""),
            "specialty": str(profile.specialty or ""),
            "intro": str(profile.intro or ""),
            "status": str(profile.status or ""),
            "departments": [],
        }
    if not items:
        return []

    ids = list(items.keys())
    dept_stmt = (
        select(ClinicianDepartment.clinician_id, Department.id, Department.name,
               ClinicianDepartment.is_primary)
        .join(Department, Department.id == ClinicianDepartment.department_id)
        .where(ClinicianDepartment.clinician_id.in_(ids))
    )
    if tenant_id is not None:
        dept_stmt = dept_stmt.where(
            ClinicianDepartment.tenant_id == tenant_id, Department.tenant_id == tenant_id,
        )
    if department_id is not None:
        dept_stmt = dept_stmt.where(ClinicianDepartment.department_id == department_id)

    matched: set[int] = set()
    for cid, dept_id, dept_name, is_primary in (await db.execute(dept_stmt)).all():
        cid = int(cid)
        items[cid]["departments"].append(
            {"id": int(dept_id), "name": str(dept_name), "is_primary": bool(is_primary)}
        )
        if department_id is not None:
            matched.add(cid)

    # 指定科室时只回该科室在岗医生：没有科室绑定的人在这个科室挂不上号
    out = [items[cid] for cid in ids if department_id is None or cid in matched]
    return out


async def ensure_doctor(db: AsyncSession, clinician_id: int | None) -> int | None:
    """校验"这个账号现在确实能接诊"（有档案 + 在岗 + 有角色）。

    草稿里显式传的 doctor_clinician_id 不能直接落库：前端可能传了已停诊/已回收角色的
    账号，甚至一个不存在的 id。落进去的结果是预约挂在了一个查不到人的医生名下，
    而且接诊关系也建不出来 —— 宁可当没给，退回按姓名匹配。
    """
    if not clinician_id:
        return None
    candidates = await list_directory(db)
    ok = {item["clinician_id"] for item in candidates}
    if clinician_id in ok:
        return clinician_id
    logger.warning(f"预约医生账号 {clinician_id} 不在可接诊目录（无档案/停诊/已回收角色），忽略")
    return None


async def sole_doctor(db: AsyncSession, *, department: str = "") -> dict[str, Any] | None:
    """该科室唯一在岗医生 —— 用于"患者没点名医生"时的确定性兜底。

    只有一位时不存在歧义：这位就是唯一能接诊的人。多于一位返回 None，
    因为"随便挑一个"会把患者记录授权给他没选过的医生。
    """
    if not department:
        return None
    dept_id = await department_id_of(db, department)
    if dept_id is None:
        return None
    items = await list_directory(db, department_id=dept_id)
    return items[0] if len(items) == 1 else None


async def resolve_doctor(    db: AsyncSession,
    *,
    department: str = "",
    doctor_name: str = "",
) -> int | None:
    """把预约草稿里的 (科室名, 医生姓名文本) 解析成医生账号。

    只在"唯一命中"时返回：同名/多科室同名医生一律返回 None 并记日志，
    由人工在管理端指定；宁可不自动建关系，也不能把患者授权错人。
    """
    target = normalize_doctor_name(doctor_name)
    if not target:
        return None

    dept_id = await department_id_of(db, department) if department else None
    if department and dept_id is None:
        logger.warning(f"医生解析：科室 {department!r} 不在 departments 表，跳过自动映射")
        return None

    candidates = [
        item for item in await list_directory(db, department_id=dept_id)
        if normalize_doctor_name(item["name"]) and normalize_doctor_name(item["name"]) in target
    ]
    if len(candidates) == 1:
        return candidates[0]["clinician_id"]
    if len(candidates) > 1:
        logger.warning(
            f"医生解析歧义：department={department!r} name={doctor_name!r} "
            f"命中 {[c['clinician_id'] for c in candidates]}，不自动绑定"
        )
    return None


async def link_consulted(
    db: AsyncSession,
    *,
    clinician_id: int,
    patient_id: int,
    department_name: str = "",
    department_id: int | None = None,
    granted_by: str = "system:consulted",
    commit: bool = True,
) -> ClinicianPatient | None:
    """接诊后建立责任关系（consulted）。已存在 assigned 时只补科室，不降级。"""
    if department_id is None and department_name:
        department_id = await department_id_of(db, department_name)
    if department_id is None and department_name:
        # 科室名对不上不阻断关系建立：接诊事实已经发生，医生该看到这个患者；
        # 科室只影响"这个患者在急症看板里算不算本科室"，后续可由管理端补齐。
        logger.warning(f"接诊建关系：科室 {department_name!r} 未在 departments 表中，department_id 记空")

    existing = (
        await db.execute(
            select(ClinicianPatient).where(
                ClinicianPatient.clinician_id == clinician_id,
                ClinicianPatient.patient_id == patient_id,
            )
        )
    ).scalar_one_or_none()

    if existing is not None:
        if existing.relation == RELATION_ASSIGNED:
            # 管理员主动分配的优先级更高，不被自动流程改写
            if department_id is not None and existing.department_id is None:
                existing.department_id = department_id
        else:
            existing.relation = RELATION_CONSULTED
            if department_id is not None:
                existing.department_id = department_id
            existing.granted_by = granted_by
        if commit:
            await db.commit()
            await db.refresh(existing)
        return existing

    row = ClinicianPatient(
        clinician_id=clinician_id,
        patient_id=patient_id,
        relation=RELATION_CONSULTED,
        department_id=department_id,
        granted_by=granted_by,
    )
    db.add(row)
    if commit:
        await db.commit()
        await db.refresh(row)
    return row
