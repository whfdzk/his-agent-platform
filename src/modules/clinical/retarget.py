# src/modules/clinical/retarget.py
#
# 确认前改选医生（C4 收尾）。
#
# 背景：挂号草稿现在会落到具体医生账号（doctor_clinician_id），患者端确认卡片
# 上会显示"医生：X"。但患者看到卡片时可能想换人，而此前只有 approve/reject ——
# 要换医生就得先驳回再重新对话，日期/时段/科室全得重来一遍。
#
# 这里做的事很小：只改 pending 草稿里的医生字段（payload + summary）。
# 不占号源、不建接诊关系，真正落库仍然是 approve 时由执行器完成；
# 因此改选失败不会留下半截状态。
#
# 两条硬约束：
#   1. 只能改 appointment/handoff 且还在等待确认的动作；已 approved 的改不了
#      （那条已建单，改草稿等于骗自己）。
#   2. 改选的医生必须在"本科室当前可挂号目录"里 —— 目录本身已经过滤了
#      在岗 + 有档案 + 仍有 clinician 角色，跨科室换人会把患者挂到别的科，
#      这里一律拒绝并回传可选名单。

from __future__ import annotations

from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.clinical import actions as clinical
from src.modules.clinical.model import (
    ACTION_APPOINTMENT,
    ACTION_HANDOFF,
    OPEN_STATUSES,
    ClinicalAction,
)
from src.modules.clinician.profile import department_id_of, list_directory

RETARGETABLE_TYPES = (ACTION_APPOINTMENT, ACTION_HANDOFF)


def _label(item: dict[str, Any]) -> str:
    return f"{item['name']} {item['title']}".strip()


def _swap_doctor_in_summary(summary: str, old_label: str, new_label: str) -> str:
    """把 summary 末尾的医生段换掉。

    summary 由工具按"科室｜日期 时段｜医生"拼成，医生段是最后一段。
    若末尾不是旧医生名（历史草稿格式不一致），新医生就直接追加；
    改为"待分配"时仅在末尾确为旧医生名的情况下删除该段，避免误删科室/时间。
    """
    parts = [p for p in str(summary or "").split("｜")]
    old = (old_label or "").strip()
    if old and parts and parts[-1].strip() == old:
        if new_label:
            parts[-1] = new_label
        else:
            parts.pop()
    elif new_label and new_label not in parts:
        parts.append(new_label)
    return "｜".join(parts)[:255]


async def retarget_doctor(
    db: AsyncSession,
    *,
    action_id: int,
    doctor_clinician_id: int = 0,
    commit: bool = True,
) -> ClinicalAction:
    """把待确认的挂号草稿改到指定医生；传 0 表示改回"待分配"。"""
    action = await clinical.get_action(db, action_id)
    if action.action_type not in RETARGETABLE_TYPES:
        raise clinical.ActionError(
            "invalid_type", f"动作 {action_id} 不是挂号动作，不能改选医生",
        )
    if await clinical.expire_if_stale(db, action, commit=False):
        await db.commit()
        raise clinical.ActionError("expired", "该确认已超时失效，请重新发起")
    if action.status not in OPEN_STATUSES:
        raise clinical.ActionError("not_open", "该动作已处理，不能再改选医生")

    payload: dict[str, Any] = dict(action.payload or {}) if isinstance(action.payload, dict) else {}
    dept = str(payload.get("department") or "").strip()
    if not dept:
        raise clinical.ActionError("no_department", "该草稿没有科室信息，无法改选医生")

    dept_id = await department_id_of(db, dept)
    items = await list_directory(db, department_id=dept_id) if dept_id else []

    target = int(doctor_clinician_id or 0)
    if target > 0:
        hit = next((i for i in items if int(i["clinician_id"]) == target), None)
        if hit is None:
            names = "、".join(
                (i["name"] + (f"（{i['title']}）" if i["title"] else "")) for i in items
            ) or "暂无可挂号医生"
            raise clinical.ActionError(
                "doctor_unavailable",
                f"{dept} 当前可选医生：{names}",
            )
        new_label = _label(hit)
        new_id: int | None = target
    else:
        new_label = ""
        new_id = None

    old_label = str(payload.get("doctor_name") or "")
    payload["doctor_name"] = new_label
    payload["doctor_clinician_id"] = new_id
    action.payload = payload
    action.summary = _swap_doctor_in_summary(action.summary, old_label, new_label)

    if commit:
        await db.commit()
        await db.refresh(action)
    else:
        await db.flush()
    logger.info(
        f"改选医生：action={action.id} {old_label or '待分配'} → {new_label or '待分配'} "
        f"(clinician={new_id})"
    )
    return action
