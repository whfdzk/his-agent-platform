# src/api/routers/clinical_actions.py
#
# 高危动作确认 API（B2）：
#   GET  /api/v1/clinical-actions/pending       ：当前用户待确认的动作
#   GET  /api/v1/clinical-actions/{id}          ：动作详情（本人或具备审批权限的医护）
#   POST /api/v1/clinical-actions/{id}/approve  ：确认并执行
#   POST /api/v1/clinical-actions/{id}/reject   ：拒绝
#
# 权限：患者只能操作自己的动作；医护代批除了要有 appointment:approve，
# 还必须满足 C4 的可见范围（责任患者 / 本科室高危），否则 403 —— 否则"有审批权限"
# 就等于"能翻全院患者"，与按科室/责任患者的授权模型自相矛盾。
# 幂等：重复 approve 返回当前状态而非重复建预约；重复 reject 同样幂等。

from __future__ import annotations

from fastapi import APIRouter, Depends
from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import AuthContext, require_auth
from src.core.exceptions import BizErrorCode, BizException
from src.infra.database import get_db
from src.modules.clinical import actions as clinical
from src.modules.clinical import decide, retarget
from src.modules.clinician.scope import REASON_DEPARTMENT, build_scope, can_read_event

router = APIRouter(
    prefix="/api/v1/clinical-actions",
    tags=["clinical-actions"],
    # 管理端账号不能借这个入口确认患者动作
    dependencies=[Depends(require_auth())],
)


def _forbidden(message: str) -> BizException:
    return BizException(BizErrorCode.FORBIDDEN, message, status_code=403)


async def _assert_can_decide(action, ctx: AuthContext, db: AsyncSession) -> None:
    """本人可决策；他人需 appointment:approve 且该患者在其可见范围内。"""
    if str(action.user_id or "") == ctx.subject_id:
        return
    if not ctx.has_scope("appointment:approve"):
        raise _forbidden("只能确认自己的待办动作")

    if action.patient_id is None:
        raise _forbidden("该动作未关联患者身份，医护不能代批")
    try:
        clinician_id = int(ctx.subject_id)
    except (TypeError, ValueError):
        raise _forbidden("医护身份无法解析，不能代批") from None

    scope = await build_scope(db, clinician_id)
    # 代批会真正写入业务数据，只放行责任患者；科室级只读告警不构成代批依据。
    if not scope.responsible(action.patient_id):
        raise _forbidden("该患者不在你的责任范围内，不能代批")
    logger.info(
        f"医护代批动作 id={action.id} by={ctx.subject_id} owner={action.user_id} "
        f"patient={action.patient_id}"
    )


def _raise_for_action_error(e: clinical.ActionError):
    if e.code == "not_found":
        return BizException(BizErrorCode.NOT_FOUND, "动作不存在", status_code=404)
    if e.code == "expired":
        return BizException(BizErrorCode.CONFLICT, "该确认已超时失效，请重新发起", status_code=410)
    return BizException(BizErrorCode.CONFLICT, e.message, status_code=409)


@router.get("/pending")
async def list_pending(
    ctx: AuthContext = Depends(require_auth()),
    db: AsyncSession = Depends(get_db),
):
    """当前用户待确认的动作（含已超时被惰性置为 expired 的不返回）。"""
    rows = await clinical.list_open(db, user_id=ctx.subject_id, limit=20)
    return {"items": [clinical.public_dict(r) for r in rows]}


@router.get("/{action_id}")
async def get_action(
    action_id: int,
    ctx: AuthContext = Depends(require_auth()),
    db: AsyncSession = Depends(get_db),
):
    try:
        action = await clinical.get_action(db, action_id)
    except clinical.ActionError as e:
        raise _raise_for_action_error(e) from None
    await _assert_can_decide(action, ctx, db)
    return {"item": clinical.public_dict(action)}


class RejectRequest(BaseModel):
    reason: str = Field(default="", max_length=255, description="拒绝原因")


def _actor_for(action, ctx: AuthContext) -> str:
    if str(action.user_id or "") == ctx.subject_id:
        return f"patient:{ctx.subject_id}"
    return f"clinician:{ctx.subject_id}"


@router.post("/{action_id}/approve")
async def approve_action(
    action_id: int,
    ctx: AuthContext = Depends(require_auth()),
    db: AsyncSession = Depends(get_db),
):
    """确认并执行。重复确认幂等：已 approved 的直接返回原结果，不会重复建预约。"""
    try:
        action = await clinical.get_action(db, action_id)
    except clinical.ActionError as e:
        raise _raise_for_action_error(e) from None
    await _assert_can_decide(action, ctx, db)

    try:
        executed, data = await decide.approve_action(
            db, action_id, actor=_actor_for(action, ctx),
        )
    except clinical.ActionError as e:
        raise _raise_for_action_error(e) from None

    return {
        "status": executed.status,
        "result_ref": executed.result_ref,
        "action_type": executed.action_type,
        "data": data,
    }


@router.post("/{action_id}/reject")
async def reject_action(
    action_id: int,
    req: RejectRequest | None = None,
    ctx: AuthContext = Depends(require_auth()),
    db: AsyncSession = Depends(get_db),
):
    try:
        action = await clinical.get_action(db, action_id)
    except clinical.ActionError as e:
        raise _raise_for_action_error(e) from None
    await _assert_can_decide(action, ctx, db)

    try:
        rejected = await decide.reject_action(
            db, action_id, actor=_actor_for(action, ctx),
            reason=(req.reason if req else ""),
        )
    except clinical.ActionError as e:
        raise _raise_for_action_error(e) from None

    return {"status": rejected.status, "reject_reason": rejected.reject_reason}


class RetargetDoctorRequest(BaseModel):
    doctor_clinician_id: int = Field(
        default=0, ge=0, description="目标医生账号；0 表示改回待分配",
    )


@router.post("/{action_id}/appointment-doctor")
async def retarget_appointment_doctor(
    action_id: int,
    req: RetargetDoctorRequest,
    ctx: AuthContext = Depends(require_auth()),
    db: AsyncSession = Depends(get_db),
):
    """确认前改选挂号医生。只改待确认草稿，不占号源、不建单，因此可反复调用。"""
    try:
        action = await clinical.get_action(db, action_id)
    except clinical.ActionError as e:
        raise _raise_for_action_error(e) from None
    await _assert_can_decide(action, ctx, db)

    try:
        updated = await retarget.retarget_doctor(
            db, action_id=action_id, doctor_clinician_id=req.doctor_clinician_id,
        )
    except clinical.ActionError as e:
        if e.code == "doctor_unavailable":
            raise BizException(
                BizErrorCode.PARAM_ERROR, e.message, status_code=400,
            ) from None
        if e.code == "invalid_type":
            raise BizException(
                BizErrorCode.PARAM_ERROR, e.message, status_code=400,
            ) from None
        if e.code == "no_department":
            raise BizException(
                BizErrorCode.PARAM_ERROR, e.message, status_code=400,
            ) from None
        raise _raise_for_action_error(e) from None

    return {"item": clinical.public_dict(updated)}
