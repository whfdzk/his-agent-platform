# src/api/routers/admin_skills.py
#
# 管理端：技能（SOP）管理。把「编辑 → 发布 → 下一单生效」做成运营动作。
#
# - 技能元数据（名称/简介/关联工具/启用）与正文版本分离：正文不可变，发布只改指针。
# - 发布 / 回滚都走同一个 active_version 指针，运行时（load_skill）每次查库，
#   因此无需重启，多 worker 也不会读到旧版本。
# - key 只允许小写字母/数字/下划线，且唯一：它是模型调用 load_skill 时的入参。

from __future__ import annotations

import re

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import AdminIdentity, get_current_admin
from src.core.exceptions import BizErrorCode, BizException
from src.infra.database import get_db
from src.modules.platform import skills as skill_service
from src.modules.platform.model import Skill, SkillVersion

router = APIRouter(
    prefix="/api/v1/admin/skills",
    tags=["admin-skills"],
    dependencies=[Depends(get_current_admin)],
)

KEY_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")


def _bad(message: str, status: int = 400) -> BizException:
    return BizException(BizErrorCode.PARAM_ERROR, message, status_code=status)


async def _get_skill(db: AsyncSession, skill_id: int) -> Skill:
    skill = await db.get(Skill, skill_id)
    if skill is None:
        raise BizException(BizErrorCode.NOT_FOUND, "技能不存在", status_code=404)
    return skill


class SkillCreate(BaseModel):
    key: str = Field(min_length=2, max_length=64)
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)
    tool_ids: list[str] = Field(default_factory=list)
    mount_hint: str = Field(default="", max_length=200)
    content: str = Field(default="", max_length=50_000)
    change_note: str = Field(default="", max_length=300)


class SkillMetaPatch(BaseModel):
    name: str | None = Field(default=None, max_length=100)
    description: str | None = Field(default=None, max_length=500)
    tool_ids: list[str] | None = None
    enabled: bool | None = None
    mount_hint: str | None = Field(default=None, max_length=200)


class VersionCreate(BaseModel):
    content: str = Field(min_length=1, max_length=50_000)
    change_note: str = Field(default="", max_length=300)


class PublishBody(BaseModel):
    version: int = Field(ge=1)


@router.get("")
async def list_skills(db: AsyncSession = Depends(get_db)):
    items = await skill_service.list_all(db)
    return {
        "items": items,
        "total": len(items),
        "effective": sum(1 for i in items if i["effective"]),
    }


@router.get("/{skill_id}")
async def get_skill(skill_id: int, db: AsyncSession = Depends(get_db)):
    skill = await _get_skill(db, skill_id)
    return {
        "skill": {
            "id": skill.id,
            "key": skill.key,
            "name": skill.name,
            "description": skill.description,
            "tool_ids": list(skill.tool_ids or []),
            "enabled": skill.enabled,
            "active_version": skill.active_version,
            "mount_hint": skill.mount_hint,
        },
        "versions": await skill_service.versions_of(db, skill_id),
    }


@router.post("")
async def create_skill(
    body: SkillCreate,
    admin: AdminIdentity = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    if not KEY_RE.match(body.key):
        raise _bad("key 只能是小写字母开头、由小写字母/数字/下划线组成，长度 2-64")
    try:
        skill = await skill_service.create_skill(
            db,
            key=body.key,
            name=body.name,
            description=body.description,
            tool_ids=body.tool_ids,
            mount_hint=body.mount_hint,
        )
    except IntegrityError:
        await db.rollback()
        raise _bad("key 已存在", status=409)

    version = None
    if body.content.strip():
        version = await skill_service.save_version(
            db, skill, content=body.content, change_note=body.change_note,
            created_by=admin.admin_id,
        )
    return {
        "skill": {"id": skill.id, "key": skill.key, "name": skill.name},
        "version": version.version if version else 0,
        "effective": False,
        "hint": "已创建草稿版本，需调用 /publish 才会对模型生效" if version else "尚未写正文",
    }


@router.put("/{skill_id}")
async def update_skill(
    skill_id: int,
    body: SkillMetaPatch,
    db: AsyncSession = Depends(get_db),
):
    skill = await _get_skill(db, skill_id)
    await skill_service.update_meta(
        db, skill,
        name=body.name, description=body.description, tool_ids=body.tool_ids,
        enabled=body.enabled, mount_hint=body.mount_hint,
    )
    return {"id": skill.id, "key": skill.key, "enabled": skill.enabled}


@router.post("/{skill_id}/versions")
async def add_version(
    skill_id: int,
    body: VersionCreate,
    admin: AdminIdentity = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    skill = await _get_skill(db, skill_id)
    version = await skill_service.save_version(
        db, skill, content=body.content, change_note=body.change_note,
        created_by=admin.admin_id,
    )
    return {
        "skill_id": skill.id,
        "version": version.version,
        "active_version": skill.active_version,
        "effective": False,
        "hint": "版本已保存，/publish 后才会生效",
    }


@router.post("/{skill_id}/publish")
async def publish_skill(
    skill_id: int,
    body: PublishBody,
    db: AsyncSession = Depends(get_db),
):
    skill = await _get_skill(db, skill_id)
    try:
        row = await skill_service.publish(db, skill, body.version)
    except LookupError:
        raise _bad("该版本不存在", status=404)
    return {
        "skill_id": skill.id,
        "key": skill.key,
        "active_version": row.version,
        "published_at": row.published_at,
        "effective": bool(skill.enabled),
        "hint": "" if skill.enabled else "技能当前为停用状态，发布不会生效",
    }


@router.post("/{skill_id}/rollback")
async def rollback_skill(
    skill_id: int,
    body: PublishBody,
    db: AsyncSession = Depends(get_db),
):
    """回滚 = 把 active_version 指回旧版本，正文快照本身不改。"""
    skill = await _get_skill(db, skill_id)
    latest = (
        await db.execute(
            select(func.max(SkillVersion.version)).where(SkillVersion.skill_id == skill.id)
        )
    ).scalar() or 0
    if body.version > int(latest):
        raise _bad("该版本不存在", status=404)
    try:
        row = await skill_service.publish(db, skill, body.version)
    except LookupError:
        raise _bad("该版本不存在", status=404)
    return {
        "skill_id": skill.id,
        "key": skill.key,
        "active_version": row.version,
        "published_at": row.published_at,
        "rolled_back": True,
    }
