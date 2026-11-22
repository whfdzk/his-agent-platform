# src/modules/clinical/actions.py
#
# 高危动作的状态机服务：创建草稿 → 请求确认 → 批准/拒绝/超时过期。
#
# 核心约束（顺序很重要）：
#   1. 只有 pending_confirm / draft 能决策；终态再决策一律幂等返回，不重复执行
#   2. 决策前先按 id 加行锁（SELECT ... FOR UPDATE）——否则患者连点两次"确认"，
#      两个请求会同时读到 pending、各建一条预约
#   3. 执行与状态变更在同一事务里：执行失败回滚，记录退回 pending_confirm，
#      不会出现"状态已 approved 但预约没建成"的假成功
#   4. 过期不靠定时任务：读取时惰性判定（expires_at < now 且仍是 open → expired）
#
# 执行器（executor）由调用方传入，形如 async (db, action) -> (result_ref, data)。
# 这样本模块不依赖 appointments 表之外的具体业务，也不会反向 import agents 层。

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.clinical.model import (
    ACTION_APPOINTMENT,
    OPEN_STATUSES,
    SOURCE_AGENT,
    STATUS_APPROVED,
    STATUS_EXPIRED,
    STATUS_PENDING,
    STATUS_REJECTED,
    TERMINAL_STATUSES,
    ClinicalAction,
)

# 待确认有效期：15 分钟。太短患者还在看报告就失效，太长又会让"旧意向"突然变成预约。
DEFAULT_TTL_SECONDS = 900

Executor = Callable[[AsyncSession, ClinicalAction], Awaitable[tuple[str, dict[str, Any]]]]


class ActionError(Exception):
    """状态机拒绝操作（已过期 / 已被决策 / 无权操作）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_aware(dt: datetime | None) -> datetime | None:
    """DB 驱动可能返回 naive 时间，统一按 UTC 补时区后再比较。"""
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


async def create_action(
    db: AsyncSession,
    *,
    action_type: str,
    payload: dict[str, Any] | None = None,
    summary: str = "",
    patient_id: int | None = None,
    user_id: str = "",
    session_id: str = "",
    thread_id: str = "",
    trace_id: str = "",
    source: str = SOURCE_AGENT,
    requested_by: str = "",
    status: str = STATUS_PENDING,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    note: str = "",
    commit: bool = True,
    tenant_id: int = 1,
) -> ClinicalAction:
    """创建一条待执行动作。默认直接进入 pending_confirm（draft 留给分步收集参数的场景）。"""
    if status not in (*OPEN_STATUSES,):
        raise ActionError("invalid_status", f"创建时只能使用 draft/pending_confirm，收到 {status}")

    action = ClinicalAction(
        tenant_id=int(tenant_id),
        action_type=action_type,
        status=status,
        patient_id=patient_id,
        user_id=str(user_id or "")[:100],
        session_id=str(session_id or "")[:100],
        thread_id=str(thread_id or "")[:160],
        trace_id=str(trace_id or "")[:64],
        source=source,
        requested_by=str(requested_by or "")[:64],
        summary=str(summary or "")[:255],
        payload=payload or {},
        expires_at=_now() + timedelta(seconds=max(60, int(ttl_seconds))),
        note=note,
    )
    db.add(action)
    if commit:
        await db.commit()
    else:
        await db.flush()
    logger.info(
        f"创建待确认动作 id={action.id} type={action_type} status={status} "
        f"user={user_id or '-'} session={session_id or '-'}"
    )
    return action


async def _load(db: AsyncSession, action_id: int, *, lock: bool = False) -> ClinicalAction:
    stmt = select(ClinicalAction).where(ClinicalAction.id == action_id)
    if lock:
        stmt = stmt.with_for_update()
    action = (await db.execute(stmt)).scalar_one_or_none()
    if action is None:
        raise ActionError("not_found", f"动作 {action_id} 不存在")
    return action


async def expire_if_stale(db: AsyncSession, action: ClinicalAction, *, commit: bool = True) -> bool:
    """惰性过期：open 状态且已超时 → 置 expired。返回是否真的改了状态。"""
    if action.status not in OPEN_STATUSES:
        return False
    expires_at = _as_aware(action.expires_at)
    if expires_at is None or expires_at > _now():
        return False
    action.status = STATUS_EXPIRED
    action.decided_at = _now()
    action.decided_by = "system:timeout"
    if commit:
        await db.commit()
        logger.info(f"动作 {action.id} 待确认超时，置为 expired")
    return True


async def list_open(
    db: AsyncSession,
    *,
    user_id: str | None = None,
    patient_id: int | None = None,
    thread_id: str | None = None,
    action_type: str | None = None,
    limit: int = 20,
) -> list[ClinicalAction]:
    """列出待确认动作（只返回 open 状态；顺带把已超时的惰性置 expired）。"""
    stmt = select(ClinicalAction).where(ClinicalAction.status.in_(OPEN_STATUSES))
    if user_id:
        stmt = stmt.where(ClinicalAction.user_id == str(user_id))
    if patient_id is not None:
        stmt = stmt.where(ClinicalAction.patient_id == patient_id)
    if thread_id:
        stmt = stmt.where(ClinicalAction.thread_id == thread_id)
    if action_type:
        stmt = stmt.where(ClinicalAction.action_type == action_type)
    stmt = stmt.order_by(ClinicalAction.id.desc()).limit(limit)

    rows = list((await db.execute(stmt)).scalars().all())
    live: list[ClinicalAction] = []
    expired_any = False
    for row in rows:
        if await expire_if_stale(db, row, commit=False):
            expired_any = True
        else:
            live.append(row)
    if expired_any:
        await db.commit()
    return live


async def approve(
    db: AsyncSession,
    action_id: int,
    *,
    actor: str,
    executor: Executor,
    note: str = "",
) -> tuple[ClinicalAction, dict[str, Any]]:
    """批准并执行。返回 (动作记录, 执行器产出的数据)。

    - 终态重复批准 → 幂等返回（不重复执行）
    - 已过期     → 抛 ActionError("expired")
    - 执行失败   → 事务回滚，状态回到 pending_confirm，异常向上抛
    """
    action = await _load(db, action_id, lock=True)

    if action.status == STATUS_APPROVED:
        logger.info(f"动作 {action_id} 已批准，幂等返回（ref={action.result_ref}）")
        return action, {}
    if action.status in (STATUS_REJECTED, STATUS_EXPIRED):
        raise ActionError("closed", f"动作已处于 {action.status}，不能再确认")

    if await expire_if_stale(db, action, commit=False):
        await db.commit()
        raise ActionError("expired", "该确认已超时失效，请重新发起")

    action.status = STATUS_APPROVED
    action.decided_at = _now()
    action.decided_by = str(actor or "")[:64]
    if note:
        action.note = (action.note + "\n" + note).strip()[:2000]

    result_ref, data = await executor(db, action)
    if result_ref:
        action.result_ref = str(result_ref)[:64]

    await db.commit()
    logger.info(f"动作 {action_id} 已批准执行 actor={actor or '-'} ref={action.result_ref or '-'}")
    return action, data


async def reject(
    db: AsyncSession,
    action_id: int,
    *,
    actor: str,
    reason: str = "",
) -> ClinicalAction:
    action = await _load(db, action_id, lock=True)

    if action.status == STATUS_REJECTED:
        logger.info(f"动作 {action_id} 已是 rejected，幂等返回")
        return action
    if action.status in TERMINAL_STATUSES:
        raise ActionError("closed", f"动作已处于 {action.status}，不能再拒绝")

    action.status = STATUS_REJECTED
    action.decided_at = _now()
    action.decided_by = str(actor or "")[:64]
    action.reject_reason = str(reason or "")[:255]
    await db.commit()
    logger.info(f"动作 {action_id} 已拒绝 actor={actor or '-'} reason={reason or '-'}")
    return action


async def get_action(db: AsyncSession, action_id: int) -> ClinicalAction:
    return await _load(db, action_id)


# 前端确认卡片只需要展示"约的是哪个科、哪天、哪个时段"这几个字段，
# payload 里其余内部键（trace_id / source 等）不外泄。
_PUBLIC_PAYLOAD_KEYS = (
    "department", "date", "time_slot", "doctor_name", "doctor_clinician_id",
    "diagnosis", "fee",
)


def public_dict(action: ClinicalAction) -> dict[str, Any]:
    """对外结构（不含 payload 里的内部字段，也不含 note）。"""
    raw = action.payload if isinstance(action.payload, dict) else {}
    preview = {k: raw[k] for k in _PUBLIC_PAYLOAD_KEYS if raw.get(k) is not None}
    return {
        "id": action.id,
        "action_type": action.action_type,
        "status": action.status,
        "summary": action.summary,
        "session_id": action.session_id,
        "expires_at": _as_aware(action.expires_at).isoformat() if action.expires_at else None,
        "decided_at": _as_aware(action.decided_at).isoformat() if action.decided_at else None,
        "decided_by": action.decided_by,
        "reject_reason": action.reject_reason,
        "result_ref": action.result_ref,
        "preview": preview,
        "created_at": _as_aware(action.created_at).isoformat() if action.created_at else None,
    }
