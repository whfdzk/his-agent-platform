# src/api/routers/model_config.py
#
# 模型配置管理 API（嵌入 / 聊天 / 重排 / 多模态）。
#
# 管理端接口：全部要求管理员登录（Depends(get_current_admin)）。
# chat 类型的配置即 LLM 故障转移的候选链，保存后立刻热刷新运行时缓存。

from __future__ import annotations

from fastapi import APIRouter, Depends

from src.api.deps import get_current_admin
from src.core.agent_registry import RefreshResult, refresh_agents
from src.core.exceptions import BizErrorCode, BizException
from src.core.model_registry import cached_candidates, refresh_candidates
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.infra.database import get_db
from src.modules.ops.model import ModelConfig

router = APIRouter(
    prefix="/api/v1/model-configs",
    tags=["model-configs"],
    dependencies=[Depends(get_current_admin)],
)

ALLOWED_TYPES = {"embedding", "chat", "rerank", "multimodal"}


def _serialize(m: ModelConfig, mask_key: bool = True) -> dict:
    key = m.api_key or ""
    if mask_key and key:
        key = key[:4] + "****" + key[-4:] if len(key) > 8 else "****"
    return {
        "id": m.id,
        "model_type": m.model_type,
        "name": m.name,
        "base_url": m.base_url,
        "api_key": key,
        "has_api_key": bool(m.api_key),
        "model_name": m.model_name,
        "enabled": m.enabled,
        "is_default": m.is_default,
        "priority": m.priority if m.priority is not None else 100,
        "created_at": str(m.created_at) if m.created_at else None,
    }


class ModelConfigUpsert(BaseModel):
    model_type: str = Field(..., description="embedding/chat/rerank/multimodal")
    name: str
    base_url: str = ""
    api_key: str = ""
    model_name: str
    enabled: bool = True
    is_default: bool = False
    priority: int = Field(default=100, ge=0, le=9999, description="故障转移优先级，数字越小越先尝试")


async def _hot_reload(db: AsyncSession) -> RefreshResult:
    """chat 配置变化后立即刷新运行时的候选链 / Agent 模型配置，无需重启。

    必须先提交：刷新走的是另一个数据库会话，未提交的变更（READ COMMITTED）它看不到。
    """
    await db.commit()
    await refresh_candidates()
    # Agent 可能通过 config.model_config_id 引用这条配置，模型/网关变了也要重建
    return await refresh_agents(reason="admin-model")


@router.get("")
async def list_model_configs(model_type: str | None = None, db: AsyncSession = Depends(get_db)):
    """模型配置列表，返回时 api_key 掩码。"""
    q = select(ModelConfig)
    if model_type:
        q = q.where(ModelConfig.model_type == model_type)
    rows = (await db.execute(q.order_by(ModelConfig.model_type, ModelConfig.id))).scalars().all()
    return {"model_configs": [_serialize(m) for m in rows], "total": len(rows)}


@router.post("")
async def create_model_config(req: ModelConfigUpsert, db: AsyncSession = Depends(get_db)):
    if req.model_type not in ALLOWED_TYPES:
        raise BizException(
            BizErrorCode.PARAM_ERROR, f"model_type 必须是 {ALLOWED_TYPES} 之一", status_code=400,
        )

    # 设为默认时，取消同类型其他配置的默认标记
    if req.is_default:
        existing = (await db.execute(
            select(ModelConfig).where(ModelConfig.model_type == req.model_type)
        )).scalars().all()
        for m in existing:
            m.is_default = False

    cfg = ModelConfig(**req.model_dump())
    db.add(cfg)
    await db.flush()
    reload = await _hot_reload(db)
    return {
        "message": reload.describe("创建成功"),
        "reload": reload.brief(),
        "model_config": _serialize(cfg),
    }


@router.put("/{config_id}")
async def update_model_config(config_id: int, req: ModelConfigUpsert, db: AsyncSession = Depends(get_db)):
    cfg = await db.get(ModelConfig, config_id)
    if not cfg:
        raise BizException(BizErrorCode.NOT_FOUND, "配置不存在", status_code=404)
    if req.model_type not in ALLOWED_TYPES:
        raise BizException(
            BizErrorCode.PARAM_ERROR, f"model_type 必须是 {ALLOWED_TYPES} 之一", status_code=400,
        )

    if req.is_default and req.model_type == cfg.model_type:
        existing = (await db.execute(
            select(ModelConfig).where(
                ModelConfig.model_type == req.model_type, ModelConfig.id != config_id
            )
        )).scalars().all()
        for m in existing:
            m.is_default = False

    data = req.model_dump()
    # api_key 传空字符串表示不修改（前端编辑时掩码显示）
    if not data.pop("api_key"):
        data.pop("api_key", None)
        data["api_key"] = cfg.api_key
    for k, v in data.items():
        setattr(cfg, k, v)
    await db.flush()
    reload = await _hot_reload(db)
    return {
        "message": reload.describe("更新成功"),
        "reload": reload.brief(),
        "model_config": _serialize(cfg),
    }


@router.delete("/{config_id}")
async def delete_model_config(config_id: int, db: AsyncSession = Depends(get_db)):
    cfg = await db.get(ModelConfig, config_id)
    if not cfg:
        raise BizException(BizErrorCode.NOT_FOUND, "配置不存在", status_code=404)
    await db.delete(cfg)
    await db.flush()
    reload = await _hot_reload(db)
    return {"message": reload.describe("删除成功"), "reload": reload.brief()}


@router.get("/chain")
async def failover_chain():
    """当前进程实际生效的故障转移链，用于确认配置是否被加载到运行时。"""
    chain = cached_candidates()
    return {
        "chain": [
            {"order": i, "name": c.name, "model_name": c.model_name, "base_url": c.base_url}
            for i, c in enumerate(chain)
        ],
        "total": len(chain),
    }
