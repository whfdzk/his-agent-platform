# src/agents/builder.py
#
# Agent 装配器：把「库里的配置」翻译成 langchain 的 create_agent 参数。
#
# 约定：
# - 配置来自 core/agent_registry（modules 层从 agents 表加载后注入缓存）
# - 配置缺失 / 草稿 / 停用 → 用各 worker 模块传入的代码内置默认（零回归兜底）
# - 单例缓存以「配置对象相等」作为失效条件：管理端保存后 refresh_agents() 更新缓存，
#   下一次取 Agent 时配置对象不等 → 自动重建，无需重启进程。

from __future__ import annotations

from typing import Any, Callable

from langchain.agents import create_agent
from langchain.agents.middleware import SummarizationMiddleware
from loguru import logger

from src.core.agent_registry import AgentRuntimeConfig
from src.core.config import build_llm
from src.agents.tool_registry import resolve_tools

DEFAULT_TEMPERATURE = 0.7
# 未显式配置 max_turns 时的轮次上限：单次问诊 8 轮模型↔工具往返已足够，
# 超出通常意味着调度异常（而不是业务需要）
DEFAULT_MAX_TURNS = 8


def llm_for(
    cfg: AgentRuntimeConfig,
    *,
    default_temperature: float = DEFAULT_TEMPERATURE,
    default_model: str | None = None,
) -> Any:
    """按配置构造 LLM：库里的 model_configs（含网关/密钥）优先，缺省回退 .env。"""
    return build_llm(
        temperature=cfg.temperature if cfg.temperature is not None else default_temperature,
        model=cfg.model or default_model,
        base_url=cfg.base_url or None,
        api_key=cfg.api_key or None,
    )


def apply_max_turns(agent: Any, cfg: AgentRuntimeConfig) -> Any:
    """把「最大执行轮数」落到 langgraph 的 recursion_limit 上。

    一次模型↔工具往返大约消耗 2 个图步，所以按 2 倍换算。

    配置里的 0 表示"未显式设置"，此时仍套一个保守上限（DEFAULT_MAX_TURNS）：
    此前 0 直接返回框架默认 recursion_limit=25，等于对异常调度不设限，
    长尾会持续消耗 token。
    """
    max_turns = cfg.max_turns if cfg.max_turns > 0 else DEFAULT_MAX_TURNS
    return agent.with_config({"recursion_limit": max_turns * 2})


def summarization_middleware(
    cfg: AgentRuntimeConfig,
    *,
    default_model: str | None = None,
) -> SummarizationMiddleware | None:
    """会话总结压缩中间件；配置里关掉时返回 None。"""
    settings = cfg.summarization
    if not settings.enabled:
        return None
    return SummarizationMiddleware(
        model=llm_for(cfg, default_model=default_model),
        trigger=[
            ("tokens", settings.trigger_tokens),
            ("messages", settings.trigger_messages),
        ],
        keep=("messages", settings.keep_messages),
    )


def build_worker_agent(
    cfg: AgentRuntimeConfig,
    *,
    default_prompt: str,
    default_tools: list[Any],
    default_temperature: float = DEFAULT_TEMPERATURE,
    default_model: str | None = None,
) -> Any:
    """装配一个 worker Agent（无 checkpointer / 无长期记忆，由调用方按需组合）。"""
    tools = resolve_tools(cfg.tool_ids, fallback=default_tools)
    system_prompt = cfg.system_prompt or default_prompt
    agent = create_agent(
        model=llm_for(cfg, default_temperature=default_temperature, default_model=default_model),
        tools=tools,
        system_prompt=system_prompt,
        name=cfg.agent_name,
    )
    logger.info(
        f"{cfg.code} agent 已装配：工具 {len(tools)} 个，提示词来源 "
        f"{'数据库' if cfg.system_prompt else '代码内置'}，温度 {cfg.temperature}"
    )
    return apply_max_turns(agent, cfg)


# ── 单例缓存 ─────────────────────────────────────────────────────────────

_runtime_cache: dict[str, tuple[AgentRuntimeConfig, Any]] = {}


def get_runtime_agent(code: str, build: Callable[[AgentRuntimeConfig], Any]) -> Any:
    """按 code 取单例；配置对象变化（保存/发布后刷新）时自动重建。"""
    from src.core.agent_registry import get_active_config

    cfg = get_active_config(code)
    cached = _runtime_cache.get(code)
    if cached is not None and cached[0] == cfg:
        return cached[1]

    agent = build(cfg)
    _runtime_cache[code] = (cfg, agent)
    return agent


def reset_agent_cache() -> None:
    """仅测试用 / 管理端强制重建：清空单例缓存。"""
    _runtime_cache.clear()
