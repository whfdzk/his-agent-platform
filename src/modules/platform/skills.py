# src/modules/platform/skills.py
#
# 技能（SOP）配置域：
#   skills           ：技能元数据 + 一句话简介（目录层，模型先看这个）
#   skill_versions   ：不可变正文快照（SKILL.md），发布/回滚只改 active_version
#   GET  .../admin/skills                   ：管理端列表（含未发布）
#   GET  .../admin/skills/{id}/versions     ：版本列表
#   POST .../admin/skills                   ：新建技能
#   PUT  .../admin/skills/{id}              ：改元数据（名称/简介/工具/启用）
#   POST .../admin/skills/{id}/versions     ：写一版正文（草稿，不生效）
#   POST .../admin/skills/{id}/publish      ：发布指定版本（下一单生效）
#   POST .../admin/skills/{id}/rollback     ：回滚到指定版本
#
# 运行时（src/tools/skill_tools.py）每次直接查库，不做进程内缓存：
# 发布后下一轮对话立即生效，多 worker 部署也不会出现"改了一个实例不认"。

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.platform.model import Skill, SkillVersion


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def catalog(db: AsyncSession) -> list[dict]:
    """已发布技能目录：只给简介，正文走 load_skill。"""
    rows = (
        await db.execute(
            select(Skill)
            .where(Skill.enabled.is_(True), Skill.active_version > 0)
            .order_by(Skill.id)
        )
    ).scalars().all()
    return [
        {
            "key": s.key,
            "name": s.name,
            "description": s.description,
            "tool_ids": list(s.tool_ids or []),
            "version": s.active_version,
        }
        for s in rows
    ]


async def get_body(db: AsyncSession, key: str) -> tuple[str | None, int]:
    """按 key 取当前生效正文。未发布 / 已停用返回 (None, 0)。"""
    skill = (await db.execute(select(Skill).where(Skill.key == key))).scalars().first()
    if skill is None or not skill.enabled or skill.active_version <= 0:
        return None, 0
    version = (
        await db.execute(
            select(SkillVersion).where(
                SkillVersion.skill_id == skill.id,
                SkillVersion.version == skill.active_version,
            )
        )
    ).scalars().first()
    if version is None:
        return None, 0
    return version.content, version.version


async def list_all(db: AsyncSession) -> list[dict]:
    """管理端列表：未发布也要能看到。"""
    rows = (await db.execute(select(Skill).order_by(Skill.id))).scalars().all()
    versions: dict[int, int] = {}
    if rows:
        cnt = (
            await db.execute(
                select(SkillVersion.skill_id, func.count(SkillVersion.id))
                .where(SkillVersion.skill_id.in_([r.id for r in rows]))
                .group_by(SkillVersion.skill_id)
            )
        ).all()
        versions = {sid: int(n) for sid, n in cnt}
    items = []
    for s in rows:
        items.append({
            "id": s.id,
            "key": s.key,
            "name": s.name,
            "description": s.description,
            "tool_ids": list(s.tool_ids or []),
            "enabled": s.enabled,
            "active_version": s.active_version,
            "mount_hint": s.mount_hint,
            "versions": versions.get(s.id, 0),
            "effective": bool(s.enabled and s.active_version > 0),
        })
    return items


async def versions_of(db: AsyncSession, skill_id: int) -> list[dict]:
    rows = (
        await db.execute(
            select(SkillVersion)
            .where(SkillVersion.skill_id == skill_id)
            .order_by(SkillVersion.version.desc())
        )
    ).scalars().all()
    return [
        {
            "version": v.version,
            "content": v.content,
            "change_note": v.change_note,
            "created_by": v.created_by,
            "published_at": v.published_at,
            "created_at": v.created_at.isoformat() if v.created_at else None,
        }
        for v in rows
    ]


async def create_skill(
    db: AsyncSession,
    *,
    key: str, name: str, description: str,
    tool_ids: list[str] | None, mount_hint: str = "",
) -> Skill:
    skill = Skill(
        key=key, name=name, description=description,
        tool_ids=list(tool_ids or []), mount_hint=mount_hint,
        enabled=True, active_version=0,
    )
    db.add(skill)
    await db.commit()
    await db.refresh(skill)
    return skill


async def update_meta(
    db: AsyncSession,
    skill: Skill,
    *,
    name: str | None = None,
    description: str | None = None,
    tool_ids: list[str] | None = None,
    enabled: bool | None = None,
    mount_hint: str | None = None,
) -> Skill:
    if name is not None:
        skill.name = name
    if description is not None:
        skill.description = description
    if tool_ids is not None:
        skill.tool_ids = list(tool_ids)
    if enabled is not None:
        skill.enabled = enabled
    if mount_hint is not None:
        skill.mount_hint = mount_hint
    await db.commit()
    await db.refresh(skill)
    return skill


async def save_version(
    db: AsyncSession,
    skill: Skill,
    *,
    content: str, change_note: str = "", created_by: str = "",
) -> SkillVersion:
    """写一版正文；不自动生效，等 publish。"""
    latest = (
        await db.execute(
            select(func.max(SkillVersion.version)).where(SkillVersion.skill_id == skill.id)
        )
    ).scalar() or 0
    version = SkillVersion(
        skill_id=skill.id,
        version=int(latest) + 1,
        content=content,
        change_note=change_note,
        created_by=created_by,
    )
    db.add(version)
    await db.commit()
    await db.refresh(version)
    return version


async def publish(db: AsyncSession, skill: Skill, version: int) -> SkillVersion:
    row = (
        await db.execute(
            select(SkillVersion).where(
                SkillVersion.skill_id == skill.id, SkillVersion.version == version
            )
        )
    ).scalars().first()
    if row is None:
        raise LookupError("版本不存在")
    row.published_at = _now_iso()
    skill.active_version = version
    await db.commit()
    await db.refresh(row)
    return row
