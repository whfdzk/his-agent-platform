# src/api/routers/admin_agents.py
#
# 管理端：Agent 配置的展示 / 新增 / 修改 / 发布 / 停用 / 删除。
#
# 设计要点：
# - code 与 src/agents 下的实现约定对应（supervisor / inquiry / drug / knowledge / report /
#   operation / health / guide），运行时按 code 从库里取配置装配 Agent。
# - 只有 status=published 的 Agent 才让库配置生效；草稿/停用回退代码内置默认，
#   这样「发布」是真正的生效开关，也保证误操作不会打挂线上。
# - 任何写操作提交后都刷新 core/agent_registry 缓存，下一次取 Agent 时自动重建（无需重启）。
# - 工具目录由 src/agents/tool_registry 依据运行时工具自动生成，不再手写副本。

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.agents.tool_registry import (
    skill_catalog,
    tool_catalog,
    validate_sub_agent_codes,
    validate_tool_ids,
    WORKER_TOOL_IDS,
)
from src.api.deps import get_current_admin
from src.core.agent_registry import RefreshResult, refresh_agents, runtime_status
from src.core.exceptions import BizErrorCode, BizException
from src.infra.database import get_db
from src.modules.platform.agent_provider import sync_builtin_agents_to_db
from src.modules.platform.model import (
    AGENT_STATUS_DISABLED,
    AGENT_STATUS_DRAFT,
    AGENT_STATUS_PUBLISHED,
    Agent,
)

router = APIRouter(
    prefix="/api/v1/admin/agents",
    tags=["admin-agents"],
    dependencies=[Depends(get_current_admin)],
)

ALLOWED_TYPES = {"supervisor", "worker"}
ALLOWED_STATUS = {AGENT_STATUS_DRAFT, AGENT_STATUS_PUBLISHED, AGENT_STATUS_DISABLED}


# ── 请求体 ───────────────────────────────────────────────────────────────


class SummarizationPayload(BaseModel):
    """会话总结压缩：对应 langchain SummarizationMiddleware 的阈值。"""

    enabled: bool = True
    trigger_tokens: int = Field(4000, ge=200, le=200_000)
    trigger_messages: int = Field(6, ge=2, le=200)
    keep_messages: int = Field(6, ge=1, le=200)


class AgentConfigPayload(BaseModel):
    """agents.config(JSONB) 的结构化校验。

    未知字段不丢弃（extra 由调用方回填），保证老版本前端/历史数据仍能保存。
    """

    # 提示词：prompt_id 关联 prompts 的激活版本，system_prompt 为内联文本（优先）
    prompt_id: int | None = None
    system_prompt: str = Field(default="", max_length=20_000)
    # 模型：model_config_id 关联 model_configs；model 直接指定模型名（覆盖前者）
    model_config_id: int | None = None
    model: str = Field(default="", max_length=100)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    max_turns: int = Field(default=0, ge=0, le=100, description="最大执行轮数，0=框架默认")
    # 能力
    tool_ids: list[str] = Field(default_factory=list, description="可使用的工具 name")
    skill_ids: list[str] = Field(default_factory=list, description="能力标签，仅用于分组展示")
    sub_agent_codes: list[str] = Field(default_factory=list, description="Supervisor 可调度的 worker")
    degrade_message: str = Field(default="", max_length=500)
    summarization: SummarizationPayload = Field(default_factory=SummarizationPayload)


class AgentUpsert(BaseModel):
    code: str = Field(..., max_length=64, description="唯一编码")
    name: str = Field(..., max_length=100)
    agent_type: str = Field(default="worker")
    description: str = Field(default="", max_length=500)
    avatar: str = ""
    config: dict | None = None


# ── 工具函数 ─────────────────────────────────────────────────────────────


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(v.strip() for v in values if v and v.strip()))


def normalize_agent_config(raw: dict | None) -> dict | None:
    """校验并规范化 agents.config。

    - 结构/取值范围不合法 → 400（提示具体字段）
    - 工具 id / 子 Agent code 不认识 → 400（避免保存后静默失效）
    - 未定义的扩展字段原样保留，避免前后端版本不一致时丢配置
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise BizException(BizErrorCode.PARAM_ERROR, "config 必须是对象", status_code=400)

    try:
        payload = AgentConfigPayload.model_validate(raw)
    except ValidationError as e:
        first = e.errors()[0]
        location = ".".join(str(item) for item in first.get("loc", ()))
        raise BizException(
            BizErrorCode.PARAM_ERROR,
            f"config.{location or '数据'} 不合法：{first.get('msg', '')}",
            status_code=400,
        ) from None

    tool_ids = _dedupe(payload.tool_ids)
    unknown_tools = validate_tool_ids(tool_ids)
    if unknown_tools:
        raise BizException(
            BizErrorCode.PARAM_ERROR, f"未知工具：{', '.join(unknown_tools)}", status_code=400,
        )

    sub_agent_codes = _dedupe(payload.sub_agent_codes)
    unknown_agents = validate_sub_agent_codes(sub_agent_codes)
    if unknown_agents:
        raise BizException(
            BizErrorCode.PARAM_ERROR,
            f"未知子 Agent：{', '.join(unknown_agents)}（可选：{', '.join(WORKER_TOOL_IDS)}）",
            status_code=400,
        )

    data = payload.model_dump()
    data["tool_ids"] = tool_ids
    data["sub_agent_codes"] = sub_agent_codes
    data["skill_ids"] = _dedupe(payload.skill_ids)
    # 回填未在 schema 中声明的扩展字段
    for key, value in raw.items():
        data.setdefault(key, value)
    return data


def _serialize(a: Agent) -> dict:
    return {
        "id": a.id,
        "code": a.code,
        "name": a.name,
        "agent_type": a.agent_type,
        "description": a.description,
        "avatar": a.avatar,
        "status": a.status,
        # 库配置是否已作用到运行时（草稿/停用时运行时仍用代码内置默认）
        "config_source": "database" if a.status == AGENT_STATUS_PUBLISHED else "builtin",
        "config": a.config,
        "published_at": a.published_at,
        "created_at": str(a.created_at) if a.created_at else None,
        "updated_at": str(a.updated_at) if a.updated_at else None,
    }


async def _validate_references(db: AsyncSession, config: dict | None) -> None:
    """保存前校验引用是否可解析。

    库里的配置能被读出来 ≠ 能装配起来：prompt_id 指向已停用的提示词、
    model_config_id 指向 embedding 模型，这类配置保存成功但运行时只会静默降级。
    这里提前拦住，把问题以 400 + 字段名返回给管理端。
    """
    if not config:
        return

    from src.modules.ops.model import ModelConfig
    from src.modules.platform.model import Prompt

    prompt_id = config.get("prompt_id")
    if isinstance(prompt_id, int) and prompt_id > 0:
        prompt = await db.get(Prompt, prompt_id)
        if prompt is None:
            raise BizException(
                BizErrorCode.PARAM_ERROR, f"config.prompt_id={prompt_id} 不存在", status_code=400,
            )
        if not prompt.enabled:
            raise BizException(
                BizErrorCode.PARAM_ERROR, f"提示词「{prompt.name}」已停用，请先启用后再绑定", status_code=400,
            )
        if not prompt.active_version:
            raise BizException(
                BizErrorCode.PARAM_ERROR,
                f"提示词「{prompt.name}」没有激活版本，请先创建并激活一个版本",
                status_code=400,
            )

    model_config_id = config.get("model_config_id")
    if isinstance(model_config_id, int) and model_config_id > 0:
        model = await db.get(ModelConfig, model_config_id)
        if model is None:
            raise BizException(
                BizErrorCode.PARAM_ERROR,
                f"config.model_config_id={model_config_id} 不存在", status_code=400,
            )
        if model.model_type != "chat":
            raise BizException(
                BizErrorCode.PARAM_ERROR,
                f"模型「{model.name}」类型为 {model.model_type}，Agent 只能绑定 chat 模型",
                status_code=400,
            )


async def _commit_and_hot_reload(db: AsyncSession) -> RefreshResult:
    """先提交再刷新运行时缓存，并返回重载结果供接口回执。

    必须显式 commit：刷新走的是另一个会话，未提交的变更（READ COMMITTED）
    在新会话里看不到，会白刷一遍旧配置。
    """
    await db.commit()
    return await refresh_agents(reason="admin")


def _reload_message(result: RefreshResult, ok_text: str) -> str:
    """统一的写操作回执文案（实现在 RefreshResult.describe，避免多处口径不一致）。"""
    return result.describe(ok_text)


# ── 接口 ─────────────────────────────────────────────────────────────────


@router.get("")
async def list_agents(
    agent_type: str | None = None,
    status: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    q = select(Agent)
    if agent_type:
        q = q.where(Agent.agent_type == agent_type)
    if status:
        q = q.where(Agent.status == status)
    rows = (await db.execute(q.order_by(Agent.id))).scalars().all()
    return {"agents": [_serialize(a) for a in rows], "total": len(rows)}


@router.get("/catalog")
async def get_agent_catalog(db: AsyncSession = Depends(get_db)):
    """可配置的工具 / 技能 / 可调度子 Agent（工具目录来自运行时注册表）。"""
    rows = (await db.execute(select(Agent).order_by(Agent.id))).scalars().all()
    workers = [
        {"code": a.code, "name": a.name}
        for a in rows
        if a.agent_type == "worker"
    ]
    return {
        "tools": tool_catalog(),
        "skills": skill_catalog(),
        "sub_agents": workers or [
            {"code": code, "name": code} for code in WORKER_TOOL_IDS
        ],
    }


@router.post("/sync-builtins")
async def sync_builtins(
    overwrite: bool = False,
    db: AsyncSession = Depends(get_db),
):
    """把内置 Agent 同步进库：缺失的新增，已存在的默认保留页面上的修改。"""
    result = await sync_builtin_agents_to_db(db, overwrite=overwrite)
    reload = await _commit_and_hot_reload(db)
    return {
        "message": _reload_message(
            reload, "同步完成" if not overwrite else "同步完成（已按内置定义覆盖配置）"
        ),
        "reload": reload.brief(),
        **result,
    }


@router.get("/runtime-status")
async def agent_runtime_status():
    """当前热加载状态：版本、生效时间、下次轮询时间、被拒配置与最后一次错误。

    管理端据此回答"我改的东西到底生效了没、还要等多久"，不必靠重启来验证。
    """
    return runtime_status()


@router.get("/{agent_id}")
async def get_agent(agent_id: int, db: AsyncSession = Depends(get_db)):
    agent = await db.get(Agent, agent_id)
    if not agent:
        raise BizException(BizErrorCode.NOT_FOUND, "Agent 不存在", status_code=404)
    return {"agent": _serialize(agent)}


@router.post("")
async def create_agent(req: AgentUpsert, db: AsyncSession = Depends(get_db)):
    if req.agent_type not in ALLOWED_TYPES:
        raise BizException(BizErrorCode.PARAM_ERROR, f"agent_type 必须是 {ALLOWED_TYPES} 之一", status_code=400)
    exists = (await db.execute(select(Agent).where(Agent.code == req.code).limit(1))).scalar_one_or_none()
    if exists:
        raise BizException(BizErrorCode.CONFLICT, f"编码 {req.code} 已存在", status_code=409)

    config = normalize_agent_config(req.config)
    await _validate_references(db, config)

    agent = Agent(
        code=req.code.strip(),
        name=req.name.strip(),
        agent_type=req.agent_type,
        description=req.description,
        avatar=req.avatar,
        config=config,
        status=AGENT_STATUS_DRAFT,
    )
    db.add(agent)
    try:
        await db.flush()
        # 显式刷新：id / created_at 等由数据库生成的字段需要回读，
        # 否则序列化时触发异步上下文外的懒加载（MissingGreenlet）。
        await db.refresh(agent)
    except IntegrityError:
        # 并发创建同一 code：应用层 SELECT 之后仍可能撞唯一索引
        await db.rollback()
        raise BizException(BizErrorCode.CONFLICT, f"编码 {req.code} 已存在", status_code=409) from None

    reload = await _commit_and_hot_reload(db)
    return {
        "message": _reload_message(reload, "创建成功（草稿状态，发布后生效）"),
        "reload": reload.brief(),
        "agent": _serialize(agent),
    }


@router.put("/{agent_id}")
async def update_agent(agent_id: int, req: AgentUpsert, db: AsyncSession = Depends(get_db)):
    agent = await db.get(Agent, agent_id)
    if not agent:
        raise BizException(BizErrorCode.NOT_FOUND, "Agent 不存在", status_code=404)
    if req.agent_type not in ALLOWED_TYPES:
        raise BizException(BizErrorCode.PARAM_ERROR, f"agent_type 必须是 {ALLOWED_TYPES} 之一", status_code=400)
    dup = (await db.execute(
        select(Agent).where(Agent.code == req.code, Agent.id != agent_id).limit(1)
    )).scalar_one_or_none()
    if dup:
        raise BizException(BizErrorCode.CONFLICT, f"编码 {req.code} 已存在", status_code=409)
    for k, v in req.model_dump().items():
        setattr(agent, k, v)
    config = normalize_agent_config(req.config)
    await _validate_references(db, config)
    agent.config = config
    try:
        await db.flush()
        await db.refresh(agent)
    except IntegrityError:
        await db.rollback()
        raise BizException(BizErrorCode.CONFLICT, f"编码 {req.code} 已存在", status_code=409) from None

    reload = await _commit_and_hot_reload(db)
    return {"message": _reload_message(reload, "更新成功"), "reload": reload.brief(), "agent": _serialize(agent)}


@router.post("/{agent_id}/publish")
async def publish_agent(agent_id: int, db: AsyncSession = Depends(get_db)):
    agent = await db.get(Agent, agent_id)
    if not agent:
        raise BizException(BizErrorCode.NOT_FOUND, "Agent 不存在", status_code=404)
    agent.status = AGENT_STATUS_PUBLISHED
    agent.published_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    await db.flush()
    # UPDATE 后 updated_at 由数据库 onupdate 生成且处于过期态，
    # 不 refresh 就在序列化时触发异步上下文外的懒加载（MissingGreenlet → 500）
    await db.refresh(agent)
    reload = await _commit_and_hot_reload(db)
    return {
        "message": _reload_message(reload, "发布成功"),
        "reload": reload.brief(),
        "agent": _serialize(agent),
    }


@router.post("/{agent_id}/disable")
async def disable_agent(agent_id: int, db: AsyncSession = Depends(get_db)):
    agent = await db.get(Agent, agent_id)
    if not agent:
        raise BizException(BizErrorCode.NOT_FOUND, "Agent 不存在", status_code=404)
    agent.status = AGENT_STATUS_DISABLED
    await db.flush()
    await db.refresh(agent)
    reload = await _commit_and_hot_reload(db)
    return {
        "message": _reload_message(reload, "已停用（运行时回退到内置默认配置）"),
        "reload": reload.brief(),
        "agent": _serialize(agent),
    }


@router.delete("/{agent_id}")
async def delete_agent(agent_id: int, db: AsyncSession = Depends(get_db)):
    """删除 Agent 配置。已发布的先停用再删除，避免误删线上配置。"""
    agent = await db.get(Agent, agent_id)
    if not agent:
        raise BizException(BizErrorCode.NOT_FOUND, "Agent 不存在", status_code=404)
    if agent.status == AGENT_STATUS_PUBLISHED:
        raise BizException(
            BizErrorCode.CONFLICT, "已发布的 Agent 需先停用后再删除", status_code=409,
        )
    code = agent.code
    await db.delete(agent)
    await db.flush()
    reload = await _commit_and_hot_reload(db)
    return {
        "message": _reload_message(reload, f"已删除 {code}（运行时回退到内置默认配置）"),
        "reload": reload.brief(),
    }
