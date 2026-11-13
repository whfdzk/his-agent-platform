# src/modules/platform/agent_provider.py
#
# Agent 配置的读库实现 + 内置 Agent 初始化。
#
# 由 core/agent_registry 反向调用（依赖倒置）：core 不 import 本模块，
# 而是由 main.py 的 lifespan 注入，见 set_provider —— 与 model_provider 同一套路。
#
# 数据来源：
#   agents                → code / name / agent_type / status / description / config(JSONB)
#   prompts + prompt_versions → config.prompt_id 指向的激活版本正文（作为 system_prompt）
#   model_configs         → config.model_config_id 指向的 chat 模型（model / base_url / api_key）
#
# 读库失败一律返回空列表：core 会保留上一次缓存，最坏情况退回代码内置默认。

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.agent_registry import (
    AgentLoadResult,
    AgentRuntimeConfig,
    ConfigIssue,
    parse_agent_config,
)
from src.infra.database import AsyncSessionLocal
from src.modules.ops.model import ModelConfig
from src.modules.platform.model import (
    AGENT_STATUS_PUBLISHED,
    Agent,
    Prompt,
    PromptVersion,
)

CHAT_MODEL_TYPE = "chat"


def _iso(value: Any) -> str:
    return value.isoformat(timespec="seconds") if hasattr(value, "isoformat") else str(value or "")


async def _load_prompt_contents(db: AsyncSession) -> tuple[dict[int, str], dict[int, Prompt]]:
    """返回 (prompt_id → 激活版本正文, prompt_id → Prompt)。

    两样都要：正文用于装配，Prompt 行用于判断「引用不存在 / 已停用 / 没有激活版本」。
    """
    prompts = (await db.execute(select(Prompt))).scalars().all()
    rows = (
        await db.execute(
            select(PromptVersion.prompt_id, PromptVersion.content)
            .join(Prompt, Prompt.id == PromptVersion.prompt_id)
            .where(PromptVersion.version == Prompt.active_version)
        )
    ).all()
    contents = {int(pid): content or "" for pid, content in rows}
    return contents, {int(p.id): p for p in prompts}


async def _load_chat_models(db: AsyncSession) -> dict[int, ModelConfig]:
    stmt = select(ModelConfig).where(ModelConfig.model_type == CHAT_MODEL_TYPE)
    rows = (await db.execute(stmt)).scalars().all()
    return {int(m.id): m for m in rows}


def _ids(value: object) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item).strip() for item in value if isinstance(item, str) and item.strip()]


def _validate_references(
    code: str,
    data: dict,
    *,
    prompt_rows: dict[int, Prompt],
    prompt_contents: dict[int, str],
    chat_models: dict[int, ModelConfig],
) -> list[ConfigIssue]:
    """装配期校验：配置能读出来 ≠ 装配得起来。

    非致命问题（提示词/模型引用失效）只是降级到内联提示词或默认模型；
    致命问题（工具名 / 子 Agent 编码不认识）会让该 Agent 整体回退内置默认，
    避免出现「配置看着生效、实际少了一半工具」的中间态。
    """
    from src.agents.tool_registry import validate_sub_agent_codes, validate_tool_ids

    issues: list[ConfigIssue] = []

    prompt_id = data.get("prompt_id")
    if isinstance(prompt_id, int):
        prompt = prompt_rows.get(prompt_id)
        if prompt is None:
            issues.append(ConfigIssue(code, "prompt_id", f"引用的提示词 {prompt_id} 不存在，已回退内联提示词"))
        elif not prompt.enabled:
            issues.append(ConfigIssue(code, "prompt_id", f"提示词 {prompt.key} 已停用，已回退内联提示词"))
        elif not prompt_contents.get(prompt_id):
            issues.append(ConfigIssue(code, "prompt_id", f"提示词 {prompt.key} 没有激活版本，已回退内联提示词"))

    model_config_id = data.get("model_config_id")
    if isinstance(model_config_id, int) and model_config_id not in chat_models:
        issues.append(ConfigIssue(
            code, "model_config_id",
            f"引用的模型配置 {model_config_id} 不存在或不是 chat 类型，已回退默认模型",
        ))

    unknown_tools = validate_tool_ids(_ids(data.get("tool_ids")))
    if unknown_tools:
        issues.append(ConfigIssue(
            code, "tool_ids", f"引用了未知工具：{', '.join(unknown_tools)}", fatal=True,
        ))

    unknown_agents = validate_sub_agent_codes(_ids(data.get("sub_agent_codes")))
    if unknown_agents:
        issues.append(ConfigIssue(
            code, "sub_agent_codes", f"引用了未知子 Agent：{', '.join(unknown_agents)}", fatal=True,
        ))

    return issues


async def load_agent_configs() -> AgentLoadResult:
    """读取全部 Agent 配置并装配成运行时结构（含 prompts / model_configs 的关联）。

    读库失败**向上抛**，不返回空列表：空列表会被 registry 当成「配置被清空」，
    从而把线上正在用的配置一起清掉。抛出去由 registry 保留上一次好配置。
    """
    async with AsyncSessionLocal() as db:
        agents = (await db.execute(select(Agent).order_by(Agent.id))).scalars().all()
        if not agents:
            return AgentLoadResult(configs=())
        prompt_contents, prompt_rows = await _load_prompt_contents(db)
        chat_models = await _load_chat_models(db)

    configs: list[AgentRuntimeConfig] = []
    issues: list[ConfigIssue] = []
    for agent in agents:
        data = agent.config if isinstance(agent.config, dict) else {}
        prompt_id = data.get("prompt_id")
        model_config_id = data.get("model_config_id")
        model_row = chat_models.get(model_config_id) if isinstance(model_config_id, int) else None

        # 草稿/停用的配置不参与运行时，引用失效也不必告警（发布时才有意义）
        if (agent.status or "draft") == AGENT_STATUS_PUBLISHED:
            issues.extend(_validate_references(
                agent.code, data,
                prompt_rows=prompt_rows, prompt_contents=prompt_contents, chat_models=chat_models,
            ))

        configs.append(
            parse_agent_config(
                agent.code,
                agent.config,
                name=agent.name or "",
                agent_type=agent.agent_type or "worker",
                status=agent.status or "draft",
                description=agent.description or "",
                prompt_content=prompt_contents.get(prompt_id, "") if isinstance(prompt_id, int) else "",
                model_name=(model_row.model_name if model_row else ""),
                model_base_url=(model_row.base_url if model_row else ""),
                model_api_key=(model_row.api_key if model_row else ""),
                revision=_iso(agent.updated_at),
            )
        )
    return AgentLoadResult(configs=tuple(configs), issues=tuple(issues))


# ── 内置 Agent 定义 ──────────────────────────────────────────────────────
#
# 这些定义只是「首次入库的初始值」：入库后以数据库为准，页面上改什么就是什么。
# 代码里的 *_SYSTEM_PROMPT 常量退化为兜底（库被清空 / 配置被删时使用）。


def builtin_agent_definitions() -> list[dict[str, Any]]:
    from src.agents.supervisor_agent import SUPERVISOR_SYSTEM_PROMPT
    from src.agents.workers.drug_agent import DRUG_SYSTEM_PROMPT
    from src.agents.workers.guide_agent import GUIDE_SYSTEM_PROMPT
    from src.agents.workers.health_agent import HEALTH_SYSTEM_PROMPT
    from src.agents.workers.inquiry_agent import INQUIRY_SYSTEM_PROMPT
    from src.agents.workers.knowledge_agent import KNOWLEDGE_SYSTEM_PROMPT
    from src.agents.workers.operation_agent import OPERATION_SYSTEM_PROMPT
    from src.agents.workers.report_agent import REPORT_SYSTEM_PROMPT
    from src.agents.tool_registry import WORKER_TOOL_IDS

    return [
        {
            "code": "supervisor",
            "name": "总助手（Supervisor）",
            "agent_type": "supervisor",
            "description": "识别用户意图，调度各专项助手并整合结果",
            "system_prompt": SUPERVISOR_SYSTEM_PROMPT,
            "temperature": 0.1,
            "tool_ids": ["save_memory", "search_memory", "get_my_profile"],
            "sub_agent_codes": list(WORKER_TOOL_IDS.keys()),
            "degrade_message": "",
            "summarization": {
                "enabled": True,
                "trigger_tokens": 4000,
                "trigger_messages": 6,
                "keep_messages": 6,
            },
        },
        {
            "code": "inquiry",
            "name": "智慧问诊",
            "agent_type": "worker",
            "description": "症状收集、分诊建议与挂号移交",
            "system_prompt": INQUIRY_SYSTEM_PROMPT,
            "temperature": 0.3,
            "tool_ids": [],
            "degrade_message": "问诊服务暂时繁忙，请稍后重试；如为紧急情况请及时线下就医。",
        },
        {
            "code": "report",
            "name": "报告解读",
            "agent_type": "worker",
            "description": "解读检验报告与影像报告的关键指标",
            "system_prompt": REPORT_SYSTEM_PROMPT,
            "temperature": 0.1,
            "tool_ids": ["get_recent_reports"],
            "degrade_message": "报告解读服务暂时繁忙，请稍后重试；如为紧急情况请及时线下就医。",
        },
        {
            "code": "drug",
            "name": "用药咨询",
            "agent_type": "worker",
            "description": "药品说明书、过敏风险核查、用药记录与处方审查",
            "system_prompt": DRUG_SYSTEM_PROMPT,
            "temperature": 0.1,
            "tool_ids": [
                "query_drug_info",
                "check_drug_allergy_risk",
                "manage_medication_record",
                "review_prescription_tool",
                "search_knowledge_graph",
            ],
            "degrade_message": "用药咨询服务暂时繁忙，请稍后重试；用药安全相关疑问请咨询药师或医生。",
        },
        {
            "code": "knowledge",
            "name": "医学知识问答",
            "agent_type": "worker",
            "description": "检索医学文档库与知识图谱回答专业问题",
            "system_prompt": KNOWLEDGE_SYSTEM_PROMPT,
            "temperature": 0.7,
            "tool_ids": [
                "search_knowledge_docs",
                "search_knowledge_graph",
                "search_knowledge_sql",
                "search_knowledge_multi",
                "review_prescription_tool",
                "list_knowledge_sources",
            ],
            "degrade_message": "医学知识问答服务暂时繁忙，请稍后重试。",
        },
        {
            "code": "operation",
            "name": "运营数据查询",
            "agent_type": "worker",
            "description": "面向内部运营人员的统计数据查询",
            "system_prompt": OPERATION_SYSTEM_PROMPT,
            "temperature": 0.7,
            "tool_ids": ["search_knowledge_sql"],
            "degrade_message": "运营数据查询服务暂时繁忙，请稍后重试。",
        },
        {
            "code": "health",
            "name": "健康管理",
            "agent_type": "worker",
            "description": "记录健康指标、查看趋势、健康计划与提醒",
            "system_prompt": HEALTH_SYSTEM_PROMPT,
            "temperature": 0.1,
            "tool_ids": [
                "record_vital_metric",
                "get_metric_trends",
                "manage_health_plan",
                "manage_reminder",
                "generate_period_report",
            ],
            "degrade_message": "健康管理服务暂时繁忙，请稍后重试；指标异常或有不适请及时线下就医。",
        },
        {
            "code": "guide",
            "name": "就医辅助",
            "agent_type": "worker",
            "description": "科室/医院信息、就诊准备、资料整理与预约挂号",
            "system_prompt": GUIDE_SYSTEM_PROMPT,
            "temperature": 0.1,
            "tool_ids": [
                "query_department_info",
                "recommend_department",
                "get_visit_preparation",
                "organize_visit_materials",
                "query_hospital_info",
                "manage_appointment",
            ],
            "degrade_message": "就医辅助服务暂时繁忙，请稍后重试；急症请直接前往急诊或拨打 120。",
        },
    ]


def builtin_agent_config(definition: dict[str, Any]) -> dict[str, Any]:
    """内置定义 → agents.config 的 JSON 结构（去掉 code/name 等列字段）。"""
    config: dict[str, Any] = {
        "system_prompt": definition.get("system_prompt", ""),
        "prompt_id": None,
        "model_config_id": None,
        "model": "",
        "temperature": definition.get("temperature"),
        # 0 表示"交给运行时默认上限"（builder.DEFAULT_MAX_TURNS），不是"不限"
        "max_turns": 0,
        "tool_ids": list(definition.get("tool_ids") or []),
        "skill_ids": [],
        "sub_agent_codes": list(definition.get("sub_agent_codes") or []),
        "degrade_message": definition.get("degrade_message", ""),
    }
    if definition.get("summarization"):
        config["summarization"] = dict(definition["summarization"])
    return config


async def sync_builtin_agents_to_db(
    db: AsyncSession,
    *,
    overwrite: bool = False,
) -> dict[str, list[str]]:
    """把内置 Agent 同步进 agents 表（幂等）。

    - 缺失的 code：新增，状态直接置为 published（保证升级后行为不变）
    - 已存在的 code：默认不动（避免覆盖页面上的人工修改）；
      overwrite=True 时用内置定义覆盖配置正文
    """
    definitions = builtin_agent_definitions()
    existing = {
        row.code: row
        for row in (await db.execute(select(Agent))).scalars().all()
    }

    created: list[str] = []
    updated: list[str] = []
    skipped: list[str] = []
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    for definition in definitions:
        code = definition["code"]
        current = existing.get(code)
        if current is None:
            db.add(Agent(
                code=code,
                name=definition["name"],
                agent_type=definition["agent_type"],
                description=definition.get("description", ""),
                avatar="",
                status=AGENT_STATUS_PUBLISHED,
                config=builtin_agent_config(definition),
                published_at=now,
            ))
            created.append(code)
            continue

        if not overwrite:
            skipped.append(code)
            continue

        current.name = definition["name"]
        current.agent_type = definition["agent_type"]
        current.description = definition.get("description", "")
        current.config = builtin_agent_config(definition)
        if current.status == AGENT_STATUS_PUBLISHED and not current.published_at:
            current.published_at = now
        updated.append(code)

    await db.flush()
    return {"created": created, "updated": updated, "skipped": skipped}


async def seed_builtin_agents(*, overwrite: bool = False) -> dict[str, list[str]]:
    """启动时调用的自举入口：用一个独立事务把内置 Agent 补齐。"""
    try:
        async with AsyncSessionLocal() as db:
            result = await sync_builtin_agents_to_db(db, overwrite=overwrite)
            await db.commit()
    except Exception as e:
        logger.warning(f"内置 Agent 初始化跳过（{type(e).__name__}: {e}）")
        return {"created": [], "updated": [], "skipped": []}

    if result["created"]:
        logger.info(f"内置 Agent 已入库: {result['created']}")
    return result
