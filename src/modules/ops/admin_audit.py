# src/modules/ops/admin_audit.py
#
# 管理端写操作审计的写入与查询封装。
#
# 约定：
#   1. 审计与业务写在同一事务里提交（调用方传同一个 AsyncSession，由调用方 commit）。
#      "改了但没审计" 比 "因为审计失败而改不动" 更危险：档案 status 决定患者能不能挂号，
#      事后无法回答"是谁把它停诊的"就是审计链断裂，所以宁可让这次写失败。
#   2. 快照只放本表字段，手机号/姓名等身份信息不进审计表（这里记的是对象 ID）。
#   3. 拒绝也要记：管理员尝试改别人档案被 403/400 时同样留痕，便于排查越权试探。

from __future__ import annotations

from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.ops.admin_audit_model import AdminAuditLog

ACTION_PROFILE_UPDATE = "clinician.profile.update"


def diff_fields(before: dict[str, Any] | None, after: dict[str, Any] | None) -> list[str]:
    """返回前后不一致的字段名（顺序按 after 的键）。"""
    if not after:
        return []
    before = before or {}
    return [k for k, v in after.items() if str(before.get(k, "")) != str(v)]


async def record_admin_audit(
    db: AsyncSession,
    *,
    actor: str,
    actor_name: str,
    action: str,
    target_kind: str,
    target_id: str | int,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    result: str = "ok",
    note: str = "",
    ip: str = "",
    request_id: str = "",
    commit: bool = True,
) -> AdminAuditLog:
    """写一条审计。commit=True 时立即提交（调用方业务写已先提交的情况用这个）。"""
    changed = diff_fields(before, after) if result == "ok" else []
    row = AdminAuditLog(
        actor=str(actor or "")[:100],
        actor_name=str(actor_name or "")[:100],
        action=action[:64],
        target_kind=target_kind[:32],
        target_id=str(target_id)[:64],
        before=before,
        after=after,
        changed=changed,
        result=result[:16],
        note=str(note or "")[:255],
        ip=str(ip or "")[:64],
        request_id=str(request_id or "")[:64],
    )
    db.add(row)
    if commit:
        await db.commit()
        await db.refresh(row)
    return row


async def list_audits(
    db: AsyncSession,
    *,
    target_kind: str = "",
    target_id: str | int | None = None,
    action: str = "",
    limit: int = 20,
    tenant_id: int | None = None,
) -> list[dict[str, Any]]:
    """按对象/动作倒序取审计记录（新的在前）。tenant_id 非空时只取本租户。"""
    stmt = select(AdminAuditLog)
    if tenant_id is not None:
        stmt = stmt.where(AdminAuditLog.tenant_id == tenant_id)
    if target_kind:
        stmt = stmt.where(AdminAuditLog.target_kind == target_kind)
    if target_id is not None:
        stmt = stmt.where(AdminAuditLog.target_id == str(target_id))
    if action:
        stmt = stmt.where(AdminAuditLog.action == action)
    rows = (
        await db.execute(stmt.order_by(desc(AdminAuditLog.id)).limit(max(1, min(limit, 100))))
    ).scalars().all()

    def _t(row: AdminAuditLog) -> str:
        return row.created_at.strftime("%Y-%m-%d %H:%M:%S") if row.created_at else ""

    return [
        {
            "id": int(row.id),
            "action": str(row.action),
            "actor": str(row.actor),
            "actor_name": str(row.actor_name),
            "before": row.before or {},
            "after": row.after or {},
            "changed": list(row.changed or []),
            "result": str(row.result),
            "note": str(row.note or ""),
            "ip": str(row.ip or ""),
            "created_at": _t(row),
        }
        for row in rows
    ]
