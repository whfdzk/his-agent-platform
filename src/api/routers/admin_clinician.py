# src/api/routers/admin_clinician.py
#
# 管理端「医护授权」入口（C4 补齐）：把原本只能靠脚本 / 手工 SQL 写的三件事搬进接口——
#   ① 授予 / 回收 clinician 角色
#   ② 绑定科室（医生能看本科室范围内的 red/orange 告警）
#   ③ 分配责任患者（医生能看该患者全量记录，并可代批）
#
# 授权判定仍然是 src/modules/clinician/scope.py 的唯一实现；这里只负责"写关系"。
# 写关系不做二次判定，避免管理端和医护端长出两套不同的授权语义。
#
# 为什么回收角色要连绑定一起删：
#   只删角色会留下"有绑定、没角色"的孤儿行，下次再授予时范围会被旧绑定悄悄撑大，
#   管理员看到的是"刚授权就有 12 个患者"，却找不到原因。

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import AdminIdentity, get_current_admin
from src.core.exceptions import BizErrorCode, BizException
from src.core.permissions import KIND_PATIENT, scopes_for_roles
from src.infra.database import get_db
from src.modules.clinician.model import (
    PROFILE_ACTIVE,
    PROFILE_INACTIVE,
    RELATION_ASSIGNED,
    RELATION_CONSULTED,
    ClinicianDepartment,
    ClinicianPatient,
    ClinicianProfile,
)
from src.modules.clinician.profile import (
    get_profile,
    list_directory,
    upsert_profile,
)
from src.modules.clinician.scope import link_department, link_patient
from src.modules.medical.model import Department, Patient
from src.modules.ops.admin_audit import (
    ACTION_PROFILE_UPDATE,
    list_audits,
    record_admin_audit,
)
from src.modules.rbac import service as rbac
from src.modules.rbac.model import RbacSubjectRole

router = APIRouter(
    prefix="/api/v1/admin/clinicians",
    tags=["admin-clinician"],
    dependencies=[Depends(get_current_admin)],
)

ROLE = "clinician"
RELATIONS = (RELATION_ASSIGNED, RELATION_CONSULTED)
APPROVE_SCOPE = "appointment:approve"


def _not_found(message: str) -> BizException:
    return BizException(BizErrorCode.NOT_FOUND, message, status_code=404)


def _conflict(message: str) -> BizException:
    return BizException(BizErrorCode.CONFLICT, message, status_code=409)


def _param(message: str) -> BizException:
    return BizException(BizErrorCode.PARAM_ERROR, message, status_code=400)


async def _patient_account(
    db: AsyncSession, clinician_id: int, *, tenant_id: int | None = None,
) -> Patient:
    row = (
        await db.execute(select(Patient).where(Patient.id == clinician_id))
    ).scalar_one_or_none()
    if row is None:
        raise _not_found(f"账号 {clinician_id} 不存在")
    # C1：管理端切到别的租户后，不能按 id 操作不属于该租户的账号（按 404 处理）
    if tenant_id is not None and int(row.tenant_id or 1) != int(tenant_id):
        raise _not_found(f"账号 {clinician_id} 不存在")
    return row


async def _require_role(db: AsyncSession, clinician_id: int) -> None:
    """绑定关系必须挂在一个真有医生角色的账号上，否则绑定永远不会生效。"""
    has = (
        await db.execute(
            select(RbacSubjectRole.id).where(
                RbacSubjectRole.subject_kind == KIND_PATIENT,
                RbacSubjectRole.subject_id == clinician_id,
                RbacSubjectRole.role_code == ROLE,
            ).limit(1)
        )
    ).scalar_one_or_none()
    if has is None:
        raise _conflict("该账号还没有医生（clinician）角色，请先授予角色再绑定范围")


async def _bindings(
    db: AsyncSession, clinician_ids: list[int], *, tenant_id: int | None = None,
) -> tuple[dict[int, list[dict]], dict[int, int]]:
    """批量取科室绑定与责任患者数（列表页一次查完，避免 N+1）。"""
    depts: dict[int, list[dict]] = {cid: [] for cid in clinician_ids}
    counts: dict[int, int] = {cid: 0 for cid in clinician_ids}
    if not clinician_ids:
        return depts, counts

    dept_stmt = (
        select(ClinicianDepartment.clinician_id, Department.id, Department.name,
               ClinicianDepartment.is_primary)
        .join(Department, Department.id == ClinicianDepartment.department_id)
        .where(ClinicianDepartment.clinician_id.in_(clinician_ids))
    )
    if tenant_id is not None:
        dept_stmt = dept_stmt.where(
            ClinicianDepartment.tenant_id == tenant_id, Department.tenant_id == tenant_id,
        )
    rows = (await db.execute(dept_stmt)).all()
    for cid, dept_id, name, is_primary in rows:
        depts[int(cid)].append(
            {"id": int(dept_id), "name": str(name), "is_primary": bool(is_primary)}
        )

    pat_stmt = (
        select(ClinicianPatient.clinician_id, ClinicianPatient.patient_id)
        .where(ClinicianPatient.clinician_id.in_(clinician_ids))
    )
    if tenant_id is not None:
        pat_stmt = pat_stmt.where(ClinicianPatient.tenant_id == tenant_id)
    for cid, _pid in (await db.execute(pat_stmt)).all():
        counts[int(cid)] += 1
    return depts, counts


class RoleGrant(BaseModel):
    note: str = Field(default="", max_length=255)


class DepartmentLink(BaseModel):
    department_id: int = Field(gt=0)
    is_primary: bool = False


class PatientLink(BaseModel):
    patient_id: int = Field(gt=0)
    relation: str = Field(default=RELATION_ASSIGNED, max_length=24)


class ProfileUpdate(BaseModel):
    """医生档案入参。全部可选：管理端抽屉是"改哪项传哪项"。

    status 只收 active/inactive：停诊用档案状态表达，不是停账号 ——
    停账号会让这个医生连自己的历史患者都登不进来。
    """

    employee_no: str | None = Field(default=None, max_length=32)
    title: str | None = Field(default=None, max_length=64)
    specialty: str | None = Field(default=None, max_length=128)
    intro: str | None = Field(default=None, max_length=255)
    status: str | None = Field(default=None, max_length=20)
    note: str | None = Field(default=None, max_length=255)


def _profile_payload(row) -> dict:
    if row is None:
        return {}
    return {
        "employee_no": str(row.employee_no or ""),
        "title": str(row.title or ""),
        "specialty": str(row.specialty or ""),
        "intro": str(row.intro or ""),
        "status": str(row.status or ""),
        "note": str(row.note or ""),
    }


# ---------------------------------------------------------------- 读


@router.get("/options")
async def list_options(
    admin: AdminIdentity = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """下拉选项：科室列表。患者候选走 /api/v1/admin/users（按手机号/姓名搜）。"""
    rows = (
        await db.execute(
            select(Department.id, Department.name)
            .where(Department.tenant_id == admin.tenant_id)
            .order_by(Department.id)
        )
    ).all()
    return {"departments": [{"id": int(i), "name": str(n)} for i, n in rows]}


@router.get("/directory")
async def doctor_directory(
    department_id: int | None = None,
    include_inactive: bool = False,
    admin: AdminIdentity = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """医生目录：有档案 + 在岗 + 仍有 clinician 角色的账号。

    患者端选医生也读这同一份口径（见 /api/v1/appointments/doctors）。
    必须定义在 /{clinician_id} 之前，否则 "directory" 会被当成 clinician_id → 422。
    """
    items = await list_directory(
        db, department_id=department_id, include_inactive=include_inactive,
        tenant_id=admin.tenant_id,
    )
    return {"items": items, "total": len(items)}


@router.get("")
async def list_clinicians(
    admin: AdminIdentity = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """列出所有持有 clinician 角色的账号，附带绑定概况（限本租户账号）。"""
    grants = (
        await db.execute(
            select(RbacSubjectRole.subject_id, RbacSubjectRole.granted_by)
            .where(
                RbacSubjectRole.subject_kind == KIND_PATIENT,
                RbacSubjectRole.role_code == ROLE,
            )
            .order_by(RbacSubjectRole.id)
        )
    ).all()
    ids = [int(g[0]) for g in grants]
    if not ids:
        return {"items": [], "total": 0}

    granted_by = {int(sid): str(by or "") for sid, by in grants}
    patients = {
        int(p.id): p
        for p in (
            await db.execute(
                select(Patient).where(
                    Patient.id.in_(ids), Patient.tenant_id == admin.tenant_id,
                )
            )
        ).scalars().all()
    }
    depts, counts = await _bindings(db, ids, tenant_id=admin.tenant_id)
    profiles = {
        int(p.clinician_id): p
        for p in (
            await db.execute(
                select(ClinicianProfile).where(
                    ClinicianProfile.clinician_id.in_(ids),
                    ClinicianProfile.tenant_id == admin.tenant_id,
                )
            )
        ).scalars().all()
    }

    role_map: dict[int, list[str]] = {cid: [] for cid in ids}
    for sid, code in (
        await db.execute(
            select(RbacSubjectRole.subject_id, RbacSubjectRole.role_code)
            .where(
                RbacSubjectRole.subject_kind == KIND_PATIENT,
                RbacSubjectRole.subject_id.in_(ids),
            )
        )
    ).all():
        role_map[int(sid)].append(str(code))

    items = []
    for cid in ids:
        p = patients.get(cid)
        if p is None:
            continue  # 角色残留但账号已删，列表不展示
        roles = role_map.get(cid, [])
        scopes = scopes_for_roles(roles)
        items.append(
            {
                "clinician_id": cid,
                "name": p.name,
                "phone": _mask_phone(p.phone),
                "enabled": bool(p.enabled),
                "roles": roles,
                "can_decide": APPROVE_SCOPE in scopes,
                "departments": depts.get(cid, []),
                "patient_count": counts.get(cid, 0),
                "granted_by": granted_by.get(cid, ""),
                "profile": _profile_payload(profiles.get(cid)),
            }
        )
    return {"items": items, "total": len(items)}


@router.get("/{clinician_id}")
async def get_clinician(
    clinician_id: int,
    admin: AdminIdentity = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    p = await _patient_account(db, clinician_id, tenant_id=admin.tenant_id)
    roles = await rbac.roles_of(db, KIND_PATIENT, clinician_id)
    depts, counts = await _bindings(db, [clinician_id], tenant_id=admin.tenant_id)

    patient_rows = (
        await db.execute(
            select(ClinicianPatient, Patient.name)
            .join(Patient, Patient.id == ClinicianPatient.patient_id)
            .where(
                ClinicianPatient.clinician_id == clinician_id,
                ClinicianPatient.tenant_id == admin.tenant_id,
            )
            .order_by(ClinicianPatient.id)
        )
    ).all()

    return {
        "clinician_id": clinician_id,
        "name": p.name,
        "phone": _mask_phone(p.phone),
        "enabled": bool(p.enabled),
        "roles": roles,
        "can_decide": APPROVE_SCOPE in scopes_for_roles(roles),
        "departments": depts.get(clinician_id, []),
        "patient_count": counts.get(clinician_id, 0),
        "profile": _profile_payload(await get_profile(db, clinician_id)),
        "patients": [
            {
                "patient_id": int(row.patient_id),
                "name": str(name),
                "relation": str(row.relation),
                "granted_by": str(row.granted_by or ""),
            }
            for row, name in patient_rows
        ],
    }


# ---------------------------------------------------------------- 角色


@router.post("/{clinician_id}/role")
async def grant_role(
    clinician_id: int,
    req: RoleGrant,
    admin: AdminIdentity = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    p = await _patient_account(db, clinician_id, tenant_id=admin.tenant_id)
    if not p.enabled:
        raise _conflict("该账号已停用，不能授予医生角色")
    granted = await rbac.grant_role(
        db, KIND_PATIENT, clinician_id, ROLE,
        granted_by="admin:clinician-bindings",
        note=req.note or "管理端授予",
    )
    logger.info(f"管理端授予 clinician：account={clinician_id} newly={granted}")
    return {"granted": granted, "roles": await rbac.roles_of(db, KIND_PATIENT, clinician_id)}


@router.delete("/{clinician_id}/role")
async def revoke_role(
    clinician_id: int,
    admin: AdminIdentity = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    await _patient_account(db, clinician_id, tenant_id=admin.tenant_id)
    revoked = await rbac.revoke_role(db, KIND_PATIENT, clinician_id, ROLE)
    # 角色没了，绑定范围也就没意义；一起删掉，避免下次授权把旧范围带回来
    dept_del = await db.execute(
        delete(ClinicianDepartment).where(
            ClinicianDepartment.clinician_id == clinician_id,
            ClinicianDepartment.tenant_id == admin.tenant_id,
        )
    )
    pat_del = await db.execute(
        delete(ClinicianPatient).where(
            ClinicianPatient.clinician_id == clinician_id,
            ClinicianPatient.tenant_id == admin.tenant_id,
        )
    )
    await db.commit()
    logger.info(
        f"管理端回收 clinician：account={clinician_id} revoked={revoked} "
        f"depts={dept_del.rowcount} patients={pat_del.rowcount}"
    )
    return {
        "revoked": revoked,
        "bindings_removed": {
            "departments": int(dept_del.rowcount or 0),
            "patients": int(pat_del.rowcount or 0),
        },
    }


# ---------------------------------------------------------------- 档案


@router.put("/{clinician_id}/profile")
async def update_profile(
    clinician_id: int,
    req: ProfileUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin: AdminIdentity = Depends(get_current_admin),
):
    """维护医生档案（职称 / 专长 / 工号 / 出诊状态）。

    不需要先有科室绑定：档案是"能不能被挂号"的信息，科室绑定是"能看哪些告警"的授权，
    管理员常常先补档案再排科室。但必须有 clinician 角色，否则档案会挂在一个没有医生权限的账号上。

    每次写都落一条 admin_audit_logs：改前快照、改后快照、实际变化的字段。
    档案 status 决定患者能不能挂到号，改错了必须能回答"谁在什么时候改的"。
    """
    ip = request.client.host if request.client else ""
    rid = request.headers.get("x-request-id", "")

    async def _audit(result: str, note: str, before: dict | None, after: dict | None) -> None:
        await record_admin_audit(
            db,
            actor=admin.admin_id,
            actor_name=admin.username,
            action=ACTION_PROFILE_UPDATE,
            target_kind="clinician",
            target_id=clinician_id,
            before=before,
            after=after,
            result=result,
            note=note,
            ip=ip,
            request_id=rid,
        )

    await _patient_account(db, clinician_id, tenant_id=admin.tenant_id)
    before = _profile_payload(await get_profile(db, clinician_id))
    try:
        await _require_role(db, clinician_id)
    except BizException:
        # 越权/误操作也要留痕：抽屉里选错账号、或角色刚被回收，都值得复盘
        await _audit("rejected", "该账号没有 clinician 角色", before, None)
        raise
    if req.status is not None and req.status not in (PROFILE_ACTIVE, PROFILE_INACTIVE):
        await _audit("rejected", f"status 非法：{req.status}", before, None)
        raise _param(f"status 只能是 {PROFILE_ACTIVE} / {PROFILE_INACTIVE}")
    row = await upsert_profile(
        db, clinician_id=clinician_id, fields=req.model_dump(exclude_none=True), commit=False,
    )
    # 先 flush 拿到列默认值（新建档案时 status 等由 server_default 填），再取快照；
    # 审计与业务写由 _audit 里的 commit 一次性提交 —— 不允许"档案改了但审计没落"。
    await db.flush()
    await db.refresh(row)
    after = _profile_payload(row)
    await _audit("ok", "", before, after)
    logger.info(
        f"管理端更新医生档案：account={clinician_id} status={row.status} "
        f"actor={admin.admin_id} changed={[k for k in after if str(before.get(k, '')) != str(after[k])]}"
    )
    return {"clinician_id": clinician_id, "profile": after}


@router.get("/{clinician_id}/audit")
async def clinician_audit(
    clinician_id: int,
    limit: int = 20,
    admin: AdminIdentity = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """该医生的档案变更记录（新的在前），供抽屉里展示"最近改了什么、谁改的"。"""
    items = await list_audits(
        db, target_kind="clinician", target_id=clinician_id, limit=limit,
        tenant_id=admin.tenant_id,
    )
    return {"items": items, "total": len(items)}



# ---------------------------------------------------------------- 科室 / 患者


@router.post("/{clinician_id}/departments")
async def bind_department(
    clinician_id: int,
    req: DepartmentLink,
    admin: AdminIdentity = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    await _patient_account(db, clinician_id, tenant_id=admin.tenant_id)
    await _require_role(db, clinician_id)
    dept = (
        await db.execute(
            select(Department).where(
                Department.id == req.department_id,
                Department.tenant_id == admin.tenant_id,
            )
        )
    ).scalar_one_or_none()
    if dept is None:
        raise _not_found(f"科室 {req.department_id} 不存在")
    row = await link_department(
        db,
        clinician_id=clinician_id,
        department_id=req.department_id,
        is_primary=req.is_primary,
        granted_by="admin:clinician-bindings",
    )
    return {"department_id": int(row.department_id), "name": str(dept.name), "is_primary": bool(row.is_primary)}


@router.delete("/{clinician_id}/departments/{department_id}")
async def unbind_department(
    clinician_id: int,
    department_id: int,
    admin: AdminIdentity = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        delete(ClinicianDepartment).where(
            ClinicianDepartment.clinician_id == clinician_id,
            ClinicianDepartment.department_id == department_id,
            ClinicianDepartment.tenant_id == admin.tenant_id,
        )
    )
    await db.commit()
    removed = int(result.rowcount or 0) > 0
    if not removed:
        raise _not_found("该科室绑定不存在")
    return {"removed": True}


@router.post("/{clinician_id}/patients")
async def bind_patient(
    clinician_id: int,
    req: PatientLink,
    admin: AdminIdentity = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    await _patient_account(db, clinician_id, tenant_id=admin.tenant_id)
    await _require_role(db, clinician_id)
    if req.relation not in RELATIONS:
        raise _param(f"relation 只能是 {RELATIONS}")
    patient = (
        await db.execute(
            select(Patient).where(
                Patient.id == req.patient_id,
                Patient.tenant_id == admin.tenant_id,
            )
        )
    ).scalar_one_or_none()
    if patient is None:
        raise _not_found(f"患者 {req.patient_id} 不存在")
    if int(patient.id) == clinician_id:
        raise _param("不能把医护账号自己设为自己的责任患者")
    row = await link_patient(
        db,
        clinician_id=clinician_id,
        patient_id=req.patient_id,
        relation=req.relation,
        granted_by="admin:clinician-bindings",
    )
    return {
        "patient_id": int(row.patient_id),
        "name": str(patient.name),
        "relation": str(row.relation),
    }


@router.delete("/{clinician_id}/patients/{patient_id}")
async def unbind_patient(
    clinician_id: int,
    patient_id: int,
    admin: AdminIdentity = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        delete(ClinicianPatient).where(
            ClinicianPatient.clinician_id == clinician_id,
            ClinicianPatient.patient_id == patient_id,
            ClinicianPatient.tenant_id == admin.tenant_id,
        )
    )
    await db.commit()
    if int(result.rowcount or 0) == 0:
        raise _not_found("该责任关系不存在")
    return {"removed": True}


def _mask_phone(phone: str | None) -> str:
    """管理端列表也只给掩码手机号，完整号码要看用户详情。"""
    if not phone:
        return ""
    phone = str(phone)
    if len(phone) < 7:
        return phone
    return f"{phone[:3]}****{phone[-4:]}"
