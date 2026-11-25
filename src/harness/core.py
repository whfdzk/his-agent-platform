# src/harness/core.py
#
# Harness 控制层门面。
#
# 为什么有这一层：此前"模型之外的那套骨架"（装配、预算、中间件、状态、事件、
# 中断编排）散在 builder.py / supervisor_agent.py / chat.py 三处，面试口径里
# 说不清"谁是 Harness"。这里不是重写，而是给现有实现一个显式入口：
#
#   调用方（chat / resume / 管理端）
#        └── AgentHarness          ← 控制层：决定用哪个 Agent、给多少预算、事件怎么发
#              ├── supervisor()     → supervisor_agent.get_supervisor_agent()
#              ├── assemble()       → builder.get_runtime_agent()（配置变了自动重建）
#              ├── budget()         → max_turns / recursion_limit / 工具次数
#              ├── emit()           → infra/event_bus（建单、结单、临床事件）
#              └── runtime_stats()  → harness/runtime 工具网关观测
#
# 分层对应：
#   Harness   = 本模块 + builder/supervisor + 中间件链
#   Agent     = src/agents/workers/*、src/agents/inquiry/*（能力单元）
#   Runtime   = src/harness/runtime.py（受控执行工具）
#   Guardrail = src/core/safety/* + src/harness/policy.py + 工具白名单
#   State     = AsyncRedisSaver checkpointer + 长期记忆 store
#   Trace     = Langfuse + 审计表 + 事件总线

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from loguru import logger


@dataclass(frozen=True)
class HarnessBudget:
    """单轮执行预算：由配置推导，调用方只读。"""

    code: str
    max_turns: int
    recursion_limit: int
    tool_max_calls: int

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "max_turns": self.max_turns,
            "recursion_limit": self.recursion_limit,
            "tool_max_calls": self.tool_max_calls,
        }


class AgentHarness:
    """控制层单例。持有租户上下文，负责把上层请求翻译成"用哪个 Agent、什么预算"。"""

    def __init__(self, *, tenant_id: int = 1):
        self.tenant_id = int(tenant_id)

    # ── 装配 ────────────────────────────────────────────────────────────
    async def supervisor(self) -> Any:
        """主控 Agent（带 checkpointer / 长期记忆 / handoff 挂起）。

        仍委托给 supervisor_agent 的单例，保证配置热更新、并发建链锁等行为不变。
        """
        from src.agents.supervisor_agent import get_supervisor_agent

        return await get_supervisor_agent()

    def assemble(self, code: str, build: Callable[[Any], Any]) -> Any:
        """按 code 装配/复用执行层 Agent：配置对象变了就重建（builder 的单例语义）。"""
        from src.agents.builder import get_runtime_agent

        return get_runtime_agent(code, build)

    # ── 预算 ────────────────────────────────────────────────────────────
    def budget(self, code: str) -> HarnessBudget:
        from src.agents.builder import DEFAULT_MAX_TURNS
        from src.core.agent_registry import get_active_config
        from src.core.config import get_settings

        cfg = get_active_config(code)
        max_turns = cfg.max_turns if cfg.max_turns > 0 else DEFAULT_MAX_TURNS
        return HarnessBudget(
            code=code,
            max_turns=max_turns,
            # 一次模型↔工具往返约 2 个图步，与 builder.apply_max_turns 保持同一口径
            recursion_limit=max_turns * 2,
            tool_max_calls=int(get_settings().TOOL_MAX_CALLS),
        )

    # ── 事件 ────────────────────────────────────────────────────────────
    async def emit(
        self,
        event_type: str,
        payload: dict | None = None,
        *,
        dedupe_key: str = "",
        tenant_id: int | None = None,
    ) -> str | None:
        """把领域事件投到总线；失败不打断主流程（publish 内部已吞异常）。"""
        from src.infra.event_bus import publish

        return await publish(
            event_type,
            payload or {},
            dedupe_key=dedupe_key,
            tenant_id=int(tenant_id if tenant_id is not None else self.tenant_id),
        )

    # ── 观测 ────────────────────────────────────────────────────────────
    def runtime_stats(self) -> dict:
        from src.harness.runtime import get_gateway

        return get_gateway().stats()

    def overview(self) -> dict:
        """给管理端的一屏快照：分层归属 + 运行时计数 + 策略。"""
        from src.agents.builder import DEFAULT_MAX_TURNS
        from src.harness import policy
        from src.harness.runtime import get_gateway

        gateway = get_gateway()
        return {
            "layers": {
                "harness": "src/harness/core.py + src/agents/builder.py + supervisor_agent.py",
                "agent": "src/agents/workers/*, src/agents/inquiry/*",
                "runtime": "src/harness/runtime.py（策略前置/超时/只读重试/异常归一）",
                "guardrail": "src/core/safety/*, src/harness/policy.py, 工具白名单",
                "state": "AsyncRedisSaver checkpointer + 长期记忆 store",
                "trace": "Langfuse + 审计表 + 事件总线",
            },
            "budget_defaults": {
                "default_max_turns": DEFAULT_MAX_TURNS,
                "recursion_limit_factor": 2,
            },
            "runtime": gateway.stats(),
            "policy": policy.snapshot(),
        }

    def log_banner(self) -> None:
        logger.info(
            f"[harness] tenant={self.tenant_id} 控制层就绪："
            "supervisor 装配、预算换算、事件投递、工具受控执行"
        )


_default_harness: AgentHarness | None = None


def get_harness(tenant_id: int = 1) -> AgentHarness:
    """进程内单例（默认租户）。需要别的租户时直接 AgentHarness(tenant_id=...)。"""
    global _default_harness
    if _default_harness is None:
        _default_harness = AgentHarness(tenant_id=tenant_id)
        _default_harness.log_banner()
    return _default_harness
