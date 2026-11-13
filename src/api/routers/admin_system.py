# src/api/routers/admin_system.py
#
# 管理端：系统配置（参数 / 开关 / 字典）。
# - 单表设计：config_type 区分渲染方式；dict 类型的 value 存 JSON 数组
# - 提供 get_public 接口供运行时读取开关（患者端不可见，仅内部服务调用）

from __future__ import annotations

import json

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_current_admin
from src.core.exceptions import BizErrorCode, BizException
from src.infra.database import get_db
from src.modules.platform.model import (
    CONFIG_TYPE_DICT,
    CONFIG_TYPE_PARAM,
    CONFIG_TYPE_SWITCH,
    SystemConfig,
)

router = APIRouter(
    prefix="/api/v1/admin/system-configs",
    tags=["admin-system-configs"],
    dependencies=[Depends(get_current_admin)],
)

ALLOWED_TYPES = {CONFIG_TYPE_PARAM, CONFIG_TYPE_SWITCH, CONFIG_TYPE_DICT}


def _serialize(c: SystemConfig) -> dict:
    value: str | bool | list = c.value or ""
    if c.config_type == CONFIG_TYPE_SWITCH:
        value = (c.value or "").lower() in ("1", "true", "on", "yes")
    elif c.config_type == CONFIG_TYPE_DICT:
        try:
            value = json.loads(c.value) if c.value else []
        except (ValueError, TypeError):
            value = []
    return {
        "id": c.id,
        "key": c.key,
        "value": value,
        "raw_value": c.value,
        "config_type": c.config_type,
        "group": c.group,
        "name": c.name,
        "description": c.description,
        "enabled": c.enabled,
        "created_at": str(c.created_at) if c.created_at else None,
        "updated_at": str(c.updated_at) if c.updated_at else None,
    }


class ConfigUpsert(BaseModel):
    key: str = Field(..., max_length=100, description="配置键，如 chat.max_turns")
    value: str = ""
    config_type: str = Field(default=CONFIG_TYPE_PARAM)
    group: str = Field(default="general", max_length=50)
    name: str = Field(..., max_length=100)
    description: str = Field(default="", max_length=500)
    enabled: bool = True


@router.get("")
async def list_configs(group: str | None = None, config_type: str | None = None, db: AsyncSession = Depends(get_db)):
    q = select(SystemConfig)
    if group:
        q = q.where(SystemConfig.group == group)
    if config_type:
        q = q.where(SystemConfig.config_type == config_type)
    rows = (await db.execute(q.order_by(SystemConfig.group, SystemConfig.id))).scalars().all()
    return {"configs": [_serialize(c) for c in rows], "total": len(rows)}


@router.post("")
async def create_config(req: ConfigUpsert, db: AsyncSession = Depends(get_db)):
    if req.config_type not in ALLOWED_TYPES:
        raise BizException(BizErrorCode.PARAM_ERROR, f"config_type 必须是 {ALLOWED_TYPES} 之一", status_code=400)
    if req.config_type == CONFIG_TYPE_DICT:
        try:
            parsed = json.loads(req.value) if req.value else []
            if not isinstance(parsed, list):
                raise ValueError
        except ValueError:
            raise BizException(BizErrorCode.PARAM_ERROR, "字典类型的值必须是 JSON 数组", status_code=400) from None
    exists = (await db.execute(select(SystemConfig).where(SystemConfig.key == req.key).limit(1))).scalar_one_or_none()
    if exists:
        raise BizException(BizErrorCode.CONFLICT, f"配置键 {req.key} 已存在", status_code=409)
    cfg = SystemConfig(**req.model_dump())
    db.add(cfg)
    await db.flush()
    return {"message": "创建成功", "config": _serialize(cfg)}


@router.put("/{config_id}")
async def update_config(config_id: int, req: ConfigUpsert, db: AsyncSession = Depends(get_db)):
    cfg = await db.get(SystemConfig, config_id)
    if not cfg:
        raise BizException(BizErrorCode.NOT_FOUND, "配置不存在", status_code=404)
    if req.config_type not in ALLOWED_TYPES:
        raise BizException(BizErrorCode.PARAM_ERROR, f"config_type 必须是 {ALLOWED_TYPES} 之一", status_code=400)
    if req.config_type == CONFIG_TYPE_DICT:
        try:
            parsed = json.loads(req.value) if req.value else []
            if not isinstance(parsed, list):
                raise ValueError
        except ValueError:
            raise BizException(BizErrorCode.PARAM_ERROR, "字典类型的值必须是 JSON 数组", status_code=400) from None
    dup = (await db.execute(
        select(SystemConfig).where(SystemConfig.key == req.key, SystemConfig.id != config_id).limit(1)
    )).scalar_one_or_none()
    if dup:
        raise BizException(BizErrorCode.CONFLICT, f"配置键 {req.key} 已存在", status_code=409)
    for k, v in req.model_dump().items():
        setattr(cfg, k, v)
    await db.flush()
    # UPDATE 后 updated_at 由数据库生成，需回读再序列化，否则异步懒加载报错
    await db.refresh(cfg)
    return {"message": "更新成功", "config": _serialize(cfg)}


@router.delete("/{config_id}")
async def delete_config(config_id: int, db: AsyncSession = Depends(get_db)):
    cfg = await db.get(SystemConfig, config_id)
    if not cfg:
        raise BizException(BizErrorCode.NOT_FOUND, "配置不存在", status_code=404)
    await db.delete(cfg)
    await db.flush()
    return {"message": "删除成功"}
