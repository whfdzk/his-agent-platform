# src/api/routers/clinician.py
#
# 医护端 API（C4 第一步：授权收口 + 三个只读视图 + 代批）
#   GET  /api/v1/clinician/scope                  ：本人可见范围（科室、责任患者数）
#   GET  /api/v1/clinician/patients               ：责任患者列表
#   GET  /api/v1/clinician/emergency-events       ：急症看板（按可见范围过滤）
#   GET  /api/v1/clinician/pending-actions        ：待确认队列（仅责任患者）
#   POST /api/v1/clinician/pending-actions/{id}/approve
#   POST /api/v1/clinician/pending-actions/{id}/reject
#
# 授权口径以 src/modules/clinician/scope.py 为唯一实现：
#   ① 责任患者 → 全等级可读
#   ② 非责任患者 → 仅"最近就诊科室 ∈ 我的绑定科室" 且事件为 red/orange
#   ③ 其余拒绝，且患者归属不明（patient_id 为空）的一律拒绝
# 代批比只读更严：只放行责任患者，科室级告警只用于"知道有危重患者要处置"。

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import AuthContext, require_auth, require_roles, require_scope
from src.core.exceptions import BizErrorCode, BizException
from src.infra.database import get_db
from src.modules.clinical import actions as clinical
from src.modules.clinical import decide
from src.modules.clinical import events as clinical_events
from src.modules.clinical.model import (
    OPEN_STATUSES,
    STATUS_APPROVED,
    STATUS_EXPIRED,
    STATUS_REJECTED,
    ClinicalAction,
)
from src.modules.clinician.model import ClinicianDepartment, ClinicianPatient
from src.modules.clinician.scope import (
    build_scope,
    can_read_event,
    recent_patient_departments,
)
from src.modules.medical.model import Patient

router = APIRouter(
    prefix="/api/v1/clinician",
    tags=["clinician"],
    # 医护身份：clinician（医生）/ nurse（护士）；管理端账号不能走这里
    dependencies=[
        Depends(require_auth()),
        Depends(require_roles("clinician", "nurse")),
        Depends(require_scope("patient:read")),
    ],
)

CANDIDATE_POOL = 200   # 事件候选池：先取最近 N 条再按可见范围过滤


def _forbidden(message: str) -> BizException:
    return BizException(BizErrorCode.FORBIDDEN, message, status_code=403)


def _raise_for_action_error(e: clinical.ActionError):
    if e.code == "not_found":
        return BizException(BizErrorCode.NOT_FOUND, "动作不存在", status_code=404)
    if e.code == "expired":
        return BizException(BizErrorCode.CONFLICT, "该确认已超时失效，请重新发起", status_code=410)
    return BizException(BizErrorCode.CONFLICT, e.message, status_code=409)


def _clinician_id(ctx: AuthContext) -> int:
    try:
        return int(ctx.subject_id)
    except (TypeError, ValueError):
        raise _forbidden("医护身份无法解析") from None


async def _empty_scope(ctx: AuthContext) -> None:
    """没有任何科室绑定与责任患者：不返回空列表，直接 403。
    空列表会让医生以为自己"没有待办"，而真实情况是"没有授权"，两者不能混淆。"""
    raise _forbidden("尚未分配科室或责任患者，请联系管理员开通")


@router.get("/scope")
async def get_scope(
    ctx: AuthContext = Depends(require_auth()),
    db: AsyncSession = Depends(get_db),
):
    clinician_id = _clinician_id(ctx)
    scope = await build_scope(db, clinician_id)

    primary = (
        await db.execute(
            select(ClinicianDepartment).where(
                ClinicianDepartment.clinician_id == clinician_id,
                ClinicianDepartment.is_primary.is_(True),
            )
        )
    ).scalars().first()

    return {
        "clinician_id": clinician_id,
        "roles": sorted(ctx.roles),
        "department_names": sorted(scope.department_names),
        "patient_ids": sorted(scope.patient_ids),
        "patient_count": len(scope.patient_ids),
        "primary_department_id": primary.department_id if primary else None,
        "can_decide_pending": ctx.has_scope("appointment:approve"),
    }


@router.get("/patients")
async def list_patients(
    limit: int = Query(default=50, ge=1, le=200),
    ctx: AuthContext = Depends(require_auth()),
    db: AsyncSession = Depends(get_db),
):
    clinician_id = _clinician_id(ctx)
    scope = await build_scope(db, clinician_id)
    if not scope.patient_ids:
        await _empty_scope(ctx)

    links = (
        await db.execute(
            select(ClinicianPatient)
            .where(
                ClinicianPatient.clinician_id == clinician_id,
                ClinicianPatient.patient_id.in_(list(scope.patient_ids)),
            )
            .order_by(ClinicianPatient.id.desc())
            .limit(limit)
        )
    ).scalars().all()
    patient_ids = [link.patient_id for link in links]

    profiles = {}
    if patient_ids:
        rows = (
            await db.execute(select(Patient).where(Patient.id.in_(patient_ids)))
        ).scalars().all()
        profiles = {p.id: p for p in rows}

    # 每个患者最近一条高危事件（用于列表上打标）
    latest = {}
    if patient_ids:
        ev_rows = (
            await db.execute(
                select(clinical_events.ClinicalEvent)
                .where(
                    clinical_events.ClinicalEvent.patient_id.in_(patient_ids),
                    clinical_events.ClinicalEvent.event_type == clinical_events.EVENT_EMERGENCY,
                    clinical_events.ClinicalEvent.level.in_(("red", "orange")),
                )
                .order_by(clinical_events.ClinicalEvent.id.desc())
            )
        ).scalars().all()
        for ev in ev_rows:
            latest.setdefault(ev.patient_id, ev)

    depts = await recent_patient_departments(db, patient_ids)

    items = []
    for link in links:
        p = profiles.get(link.patient_id)
        ev = latest.get(link.patient_id)
        items.append({
            "patient_id": link.patient_id,
            "name": p.name if p else "—",
            "gender": p.gender if p else None,
            "age": p.age if p else None,
            # 手机号属于个人信息，医护端列表不下发，需要时走详情接口
            "relation": link.relation,
            "department_ids": [link.department_id] if link.department_id else [],
            "recent_departments": sorted(depts.get(link.patient_id, set())),
            "latest_alert": None if ev is None else {
                "id": ev.id,
                "level": ev.level,
                "level_label": clinical_events.LEVEL_LABELS.get(ev.level, ev.level),
                "created_at": ev.created_at.isoformat() if ev.created_at else None,
            },
            "visible_reason": "responsible",
        })
    return {"items": items, "total": len(items)}


@router.get("/emergency-events")
async def list_emergency_events(
    session_id: str = "",
    patient_id: int | None = None,
    level: str | None = None,
    min_level: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    ctx: AuthContext = Depends(require_auth()),
    db: AsyncSession = Depends(get_db),
):
    """急症事件流。候选池先取最近 CANDIDATE_POOL 条，再逐条按可见范围过滤。"""
    clinician_id = _clinician_id(ctx)
    scope = await build_scope(db, clinician_id)
    if not scope.department_names and not scope.patient_ids:
        await _empty_scope(ctx)

    rows = await clinical_events.list_events(
        db,
        patient_id=patient_id,
        session_id=session_id,
        level=level,
        min_level=min_level,
        limit=CANDIDATE_POOL,
    )
    depts = await recent_patient_departments(
        db, [r["patient_id"] for r in rows if r.get("patient_id") is not None]
    )

    items = []
    for row in rows:
        ok, why = can_read_event(
            scope,
            patient_id=row.get("patient_id"),
            level=row.get("level") or "",
            patient_departments=depts.get(row.get("patient_id"), set()),
        )
        if not ok:
            continue
        row = dict(row)
        row["visible_reason"] = why
        items.append(row)
        if len(items) >= limit:
            break

    anywhere = len(rows) >= CANDIDATE_POOL
    return {
        "items": items,
        "total": len(items),
        # 候选池被打满时，更早的事件没有进入过滤，列表可能不是全量
        "truncated": anywhere,
        "department_names": sorted(scope.department_names),
    }


@router.get("/pending-actions")
async def list_pending_actions(
    limit: int = Query(default=20, ge=1, le=100),
    ctx: AuthContext = Depends(require_auth()),
    db: AsyncSession = Depends(get_db),
):
    """待确认队列：只列责任患者的动作（跨医生的动作不互相可见）。"""
    clinician_id = _clinician_id(ctx)
    scope = await build_scope(db, clinician_id)
    if not scope.patient_ids:
        await _empty_scope(ctx)

    # 只查责任患者自己的动作，不去碰他人数据
    stmt = (
        select(ClinicalAction)
        .where(
            ClinicalAction.status.in_(OPEN_STATUSES),
            ClinicalAction.patient_id.in_(list(scope.patient_ids)),
        )
        .order_by(ClinicalAction.id.desc())
        .limit(100)
    )
    rows = list((await db.execute(stmt)).scalars().all())
    live = []
    expired_any = False
    for row in rows:
        if await clinical.expire_if_stale(db, row, commit=False):
            expired_any = True
        else:
            live.append(row)
    if expired_any:
        await db.commit()

    items = []
    picked = []
    for row in live:
        if not scope.responsible(row.patient_id):
            continue
        picked.append(row)
        if len(picked) >= limit:
            break

    # public_dict 面向患者端，不含 patient_id；医护端必须知道"这是谁的动作"
    names: dict[int, str] = {}
    pids = [row.patient_id for row in picked if row.patient_id is not None]
    if pids:
        rows2 = (
            await db.execute(select(Patient.id, Patient.name).where(Patient.id.in_(pids)))
        ).all()
        names = {int(pid): str(name or "") for pid, name in rows2}

    for row in picked:
        item = clinical.public_dict(row)
        item["patient_id"] = row.patient_id
        item["patient_name"] = names.get(row.patient_id or 0, "—")
        items.append(item)
    return {
        "items": items,
        "total": len(items),
        "can_decide": ctx.has_scope("appointment:approve"),
    }


async def _load_decidable(db: AsyncSession, action_id: int, ctx: AuthContext):
    """医护端代批的统一前置：动作存在 + 患者属责任范围 + 具备审批权限。"""
    if not ctx.has_scope("appointment:approve"):
        raise _forbidden("当前账号无审批权限")
    try:
        action = await clinical.get_action(db, action_id)
    except clinical.ActionError as e:
        raise _raise_for_action_error(e) from None
    if action.patient_id is None:
        raise _forbidden("该动作未关联患者身份，不能代批")
    if action.status in (STATUS_APPROVED, STATUS_REJECTED, STATUS_EXPIRED):
        # 幂等重放交给 decide，不在前置里拦
        return action
    scope = await build_scope(db, _clinician_id(ctx))
    if not scope.responsible(action.patient_id):
        raise _forbidden("该患者不在你的责任范围内，不能代批")
    return action


@router.post("/pending-actions/{action_id}/approve")
async def approve_pending_action(
    action_id: int,
    ctx: AuthContext = Depends(require_auth()),
    db: AsyncSession = Depends(get_db),
):
    action = await _load_decidable(db, action_id, ctx)
    try:
        executed, data = await decide.approve_action(
            db, action_id, actor=f"clinician:{ctx.subject_id}",
        )
    except clinical.ActionError as e:
        raise _raise_for_action_error(e) from None
    logger.info(f"医护端代批 id={action_id} by={ctx.subject_id} ref={executed.result_ref or '-'}")
    return {
        "status": executed.status,
        "result_ref": executed.result_ref,
        "action_type": executed.action_type,
        "data": data,
    }


@router.post("/pending-actions/{action_id}/reject")
async def reject_pending_action(
    action_id: int,
    ctx: AuthContext = Depends(require_auth()),
    db: AsyncSession = Depends(get_db),
):
    action = await _load_decidable(db, action_id, ctx)
    try:
        rejected = await decide.reject_action(
            db, action_id, actor=f"clinician:{ctx.subject_id}", reason="医护端驳回",
        )
    except clinical.ActionError as e:
        raise _raise_for_action_error(e) from None
    logger.info(f"医护端驳回 id={action_id} by={ctx.subject_id}")
    return {"status": rejected.status, "reject_reason": rejected.reject_reason}
