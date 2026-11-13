# src/api/routers/admin_prompts.py
#
# 管理端：Prompt 版本管理。
# - 一条 Prompt（业务键唯一）挂多个不可变版本（prompt_versions）
# - 激活 / 回滚 = 切换 prompts.active_version，历史版本永不覆盖，便于审计

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import AdminIdentity, get_current_admin
from src.core.agent_registry import RefreshResult, refresh_agents
from src.core.exceptions import BizErrorCode, BizException
from src.infra.database import get_db
from src.modules.platform.model import Prompt, PromptVersion

router = APIRouter(
    prefix="/api/v1/admin/prompts",
    tags=["admin-prompts"],
    dependencies=[Depends(get_current_admin)],
)


def _serialize_prompt(p: Prompt) -> dict:
    return {
        "id": p.id,
        "key": p.key,
        "name": p.name,
        "scene": p.scene,
        "description": p.description,
        "enabled": p.enabled,
        "active_version": p.active_version,
        "created_at": str(p.created_at) if p.created_at else None,
        "updated_at": str(p.updated_at) if p.updated_at else None,
    }


def _serialize_version(v: PromptVersion) -> dict:
    return {
        "id": v.id,
        "prompt_id": v.prompt_id,
        "version": v.version,
        "content": v.content,
        "change_note": v.change_note,
        "created_by": v.created_by,
        "created_at": str(v.created_at) if v.created_at else None,
    }


class PromptCreate(BaseModel):
    key: str = Field(..., max_length=64, description="业务键，如 supervisor.system")
    name: str = Field(..., max_length=100)
    scene: str = Field(default="agent", max_length=50)
    description: str = Field(default="", max_length=500)
    first_content: str = Field(default="", description="首版内容（可空，稍后创建版本）")
    change_note: str = ""


class PromptUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=100)
    scene: str | None = Field(default=None, max_length=50)
    description: str | None = Field(default=None, max_length=500)
    enabled: bool | None = None


class VersionCreate(BaseModel):
    content: str = Field(..., min_length=1)
    change_note: str = Field(default="", max_length=300)


async def _get_prompt_or_404(db: AsyncSession, prompt_id: int) -> Prompt:
    p = await db.get(Prompt, prompt_id)
    if not p:
        raise BizException(BizErrorCode.NOT_FOUND, "Prompt 不存在", status_code=404)
    return p


async def _commit_and_refresh_agents(db: AsyncSession) -> RefreshResult:
    """提示词变更后刷新 Agent 配置缓存。

    Agent 的 system_prompt 可以来自 prompts 的激活版本，所以在提示词管理里
    改内容 / 切版本后，必须让 Agent 侧缓存失效，否则要等进程重启才生效。
    先 commit 再刷新：刷新走独立会话，未提交的变更它看不到。
    """
    await db.commit()
    return await refresh_agents(reason="admin-prompt")


@router.get("")
async def list_prompts(scene: str | None = None, db: AsyncSession = Depends(get_db)):
    q = select(Prompt)
    if scene:
        q = q.where(Prompt.scene == scene)
    rows = (await db.execute(q.order_by(Prompt.id))).scalars().all()
    return {"prompts": [_serialize_prompt(p) for p in rows], "total": len(rows)}


@router.post("")
async def create_prompt(
    req: PromptCreate, db: AsyncSession = Depends(get_db), admin: AdminIdentity = Depends(get_current_admin)
):
    exists = (await db.execute(select(Prompt).where(Prompt.key == req.key).limit(1))).scalar_one_or_none()
    if exists:
        raise BizException(BizErrorCode.CONFLICT, f"业务键 {req.key} 已存在", status_code=409)

    prompt = Prompt(
        key=req.key, name=req.name, scene=req.scene, description=req.description, active_version=0
    )
    db.add(prompt)
    await db.flush()

    if req.first_content.strip():
        version = PromptVersion(
            prompt_id=prompt.id, version=1, content=req.first_content,
            change_note=req.change_note, created_by=admin.username,
        )
        db.add(version)
        prompt.active_version = 1
    await db.flush()
    # 带上首版时 prompt 会被 UPDATE（active_version），updated_at 随之过期，需回读
    await db.refresh(prompt)
    reload = await _commit_and_refresh_agents(db)
    return {
        "message": reload.describe("创建成功"),
        "reload": reload.brief(),
        "prompt": _serialize_prompt(prompt),
    }


@router.put("/{prompt_id}")
async def update_prompt(prompt_id: int, req: PromptUpdate, db: AsyncSession = Depends(get_db)):
    prompt = await _get_prompt_or_404(db, prompt_id)
    for k, v in req.model_dump(exclude_none=True).items():
        setattr(prompt, k, v)
    await db.flush()
    # UPDATE 后 updated_at 为数据库 onupdate，需回读再序列化
    await db.refresh(prompt)
    reload = await _commit_and_refresh_agents(db)
    return {
        "message": reload.describe("更新成功"),
        "reload": reload.brief(),
        "prompt": _serialize_prompt(prompt),
    }


@router.delete("/{prompt_id}")
async def delete_prompt(prompt_id: int, db: AsyncSession = Depends(get_db)):
    prompt = await _get_prompt_or_404(db, prompt_id)
    versions = (await db.execute(
        select(PromptVersion).where(PromptVersion.prompt_id == prompt_id)
    )).scalars().all()
    for v in versions:
        await db.delete(v)
    await db.delete(prompt)
    await db.flush()
    reload = await _commit_and_refresh_agents(db)
    return {"message": reload.describe("删除成功"), "reload": reload.brief()}


@router.get("/{prompt_id}/versions")
async def list_versions(prompt_id: int, db: AsyncSession = Depends(get_db)):
    await _get_prompt_or_404(db, prompt_id)
    rows = (await db.execute(
        select(PromptVersion).where(PromptVersion.prompt_id == prompt_id).order_by(PromptVersion.version.desc())
    )).scalars().all()
    return {"versions": [_serialize_version(v) for v in rows], "total": len(rows)}


@router.post("/{prompt_id}/versions")
async def create_version(
    prompt_id: int, req: VersionCreate,
    db: AsyncSession = Depends(get_db), admin: AdminIdentity = Depends(get_current_admin),
):
    """新版本 = 当前激活内容之上递增，永不覆盖历史。"""
    prompt = await _get_prompt_or_404(db, prompt_id)
    latest = (await db.execute(
        select(PromptVersion).where(PromptVersion.prompt_id == prompt_id)
        .order_by(PromptVersion.version.desc()).limit(1)
    )).scalar_one_or_none()
    next_version = (latest.version + 1) if latest else 1
    version = PromptVersion(
        prompt_id=prompt_id, version=next_version, content=req.content,
        change_note=req.change_note, created_by=admin.username,
    )
    db.add(version)
    prompt.active_version = next_version  # 新建版本默认激活
    await db.flush()
    reload = await _commit_and_refresh_agents(db)
    return {
        "message": reload.describe(f"已创建 v{next_version} 并激活"),
        "reload": reload.brief(),
        "version": _serialize_version(version),
    }


@router.post("/{prompt_id}/versions/{version}/activate")
async def activate_version(prompt_id: int, version: int, db: AsyncSession = Depends(get_db)):
    """激活 / 回滚指定版本。"""
    prompt = await _get_prompt_or_404(db, prompt_id)
    target = (await db.execute(
        select(PromptVersion).where(
            PromptVersion.prompt_id == prompt_id, PromptVersion.version == version
        ).limit(1)
    )).scalar_one_or_none()
    if not target:
        raise BizException(BizErrorCode.NOT_FOUND, "版本不存在", status_code=404)
    prompt.active_version = version
    await db.flush()
    await db.refresh(prompt)
    reload = await _commit_and_refresh_agents(db)
    return {
        "message": reload.describe(f"已激活 v{version}"),
        "reload": reload.brief(),
        "prompt": _serialize_prompt(prompt),
    }
