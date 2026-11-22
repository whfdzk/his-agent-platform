# src/api/routers/clinical_events.py
#
# 急症分级事件查询 API（B3）：
#   GET /api/v1/clinical-events/emergency        ：分级明细
#   GET /api/v1/clinical-events/emergency/stats  ：按四档聚合计数
#
# 权限：患者只能查自己的；具备审批权或在审计范围（appointment:approve / audit:read）
# 的账号可以按 patient_id 查他人，也首次让"急症识别"从回复文案变成可核对的记录。
#
# 为什么需要接口：落库本身对患者不可见，只有医护/质控能查，才算真正"可查询、可统计、可交接"。

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import AuthContext, require_auth
from src.core.exceptions import BizErrorCode, BizException
from src.infra.database import get_db
from src.modules.clinical import events as clinical_events
from src.modules.clinical.events import LEVEL_LABELS

router = APIRouter(
    prefix="/api/v1/clinical-events",
    tags=["clinical-events"],
    dependencies=[Depends(require_auth())],
)


def _self_patient_id(ctx: AuthContext) -> int | None:
    """患者账号的 subject_id 就是 patients.id；解析失败返回 None（按用户标识兜底）。"""
    try:
        return int(ctx.subject_id)
    except (TypeError, ValueError):
        return None


def _can_read_others(ctx: AuthContext) -> bool:
    """查看他人急症记录需要医护审批权或在审计范围内。"""
    return ctx.is_admin or ctx.has_scope("appointment:approve", "audit:read")


def _forbidden(message: str) -> BizException:
    return BizException(BizErrorCode.FORBIDDEN, message, status_code=403)


@router.get("/emergency")
async def list_emergency_events(
    session_id: str = Query(default="", description="只看某个会话"),
    patient_id: int | None = Query(default=None, description="按患者过滤（需医护/审计权限）"),
    level: str = Query(default="", description="只看某档：red/orange/yellow/green"),
    min_level: str = Query(default="", description="只看不低于某档的事件"),
    limit: int = Query(default=50, ge=1, le=200),
    ctx: AuthContext = Depends(require_auth()),
    db: AsyncSession = Depends(get_db),
):
    target = patient_id
    if target is not None:
        if not _can_read_others(ctx) and target != _self_patient_id(ctx):
            raise _forbidden("只能查看自己的急症记录")
    elif not _can_read_others(ctx):
        # 患者不传 patient_id 时默认只查自己，避免默认行为变成越权
        target = _self_patient_id(ctx)

    if not _can_read_others(ctx) and session_id and target is None:
        raise _forbidden("只能查看自己的急症记录")

    items = await clinical_events.list_events(
        db,
        patient_id=target,
        session_id=session_id,
        level=(level or None),
        min_level=(min_level or None),
        limit=limit,
    )
    return {"items": items}


@router.get("/emergency/stats")
async def emergency_stats(
    session_id: str = Query(default="", description="只看某个会话"),
    patient_id: int | None = Query(default=None, description="按患者聚合（需医护/审计权限）"),
    ctx: AuthContext = Depends(require_auth()),
    db: AsyncSession = Depends(get_db),
):
    target = patient_id
    if target is not None:
        if not _can_read_others(ctx) and target != _self_patient_id(ctx):
            raise _forbidden("只能查看自己的急症记录")
    elif not _can_read_others(ctx):
        target = _self_patient_id(ctx)

    counts = await clinical_events.count_by_level(
        db, patient_id=target, session_id=session_id,
    )
    return {
        "counts": counts,
        "labels": LEVEL_LABELS,
        "total": sum(counts.values()),
    }
