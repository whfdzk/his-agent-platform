# src/core/limits.py
#
# 单轮执行的资源预算（防跑飞 / 防刷）。
#
# 上限项：
# - 最大 Agent 调用次数（supervisor + worker）
# - 最大工具调用次数
# - 最大执行时间（与 TURN_TIMEOUT 互补：这里是预算对象自己的软上限）
# - 最大 Token 数
# - 最大检索结果条数
# - 最大并发任务数（按用户，Redis 跨进程计数）
#
# 超限统一抛 BudgetExceeded，由调用方转成用户可读提示，并记录 error_type。

from __future__ import annotations

import contextvars
import time
from dataclasses import dataclass, field

from loguru import logger

from src.core.config import get_settings


class BudgetExceeded(Exception):
    """预算耗尽。kind 用于追踪里的 error_type，message 面向用户。"""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


@dataclass
class ExecutionBudget:
    """一次用户请求的执行预算。"""

    max_agent_calls: int = 12
    max_tool_calls: int = 20
    max_seconds: float = 240.0
    max_tokens: int = 60000
    max_retrieval_results: int = 8
    max_concurrent_tasks: int = 2
    started_at: float = field(default_factory=time.monotonic)
    agent_calls: int = 0
    tool_calls: int = 0
    tokens: int = 0
    tool_names: list[str] = field(default_factory=list)

    # ── 校验 ────────────────────────────────────────────────────────────
    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    def check_time(self) -> None:
        if self.elapsed() > self.max_seconds:
            raise BudgetExceeded(
                "time_budget_exceeded",
                "本次咨询耗时过长，已中止。请把问题拆小一些再试。",
            )

    def consume_agent(self, name: str = "") -> None:
        self.check_time()
        if self.agent_calls >= self.max_agent_calls:
            raise BudgetExceeded(
                "agent_calls_exceeded",
                "本次咨询的推理步骤已达上限，请补充更具体的信息后重试。",
            )
        self.agent_calls += 1

    def consume_tool(self, name: str = "") -> None:
        self.check_time()
        if self.tool_calls >= self.max_tool_calls:
            raise BudgetExceeded(
                "tool_calls_exceeded",
                "本次咨询需要查询的内容过多，请补充更具体的信息后重试。",
            )
        self.tool_calls += 1
        if name and name not in self.tool_names:
            self.tool_names.append(name)

    def add_tokens(self, tokens: int) -> None:
        self.tokens += max(0, int(tokens or 0))

    def ensure_tokens(self) -> None:
        if self.tokens > self.max_tokens:
            raise BudgetExceeded(
                "token_budget_exceeded",
                "本次咨询内容过长，已超出处理上限，请精简后重试。",
            )

    def clamp_retrieval(self, requested: int | None) -> int:
        """把检索条数收敛到预算内（调用方仍应按返回值截断）。"""
        if requested is None or requested <= 0:
            return self.max_retrieval_results
        return min(int(requested), self.max_retrieval_results)


def build_budget() -> ExecutionBudget:
    s = get_settings()
    return ExecutionBudget(
        max_agent_calls=s.AGENT_MAX_CALLS,
        max_tool_calls=s.TOOL_MAX_CALLS,
        max_seconds=s.TURN_TIMEOUT,
        max_tokens=s.TURN_MAX_TOKENS,
        max_retrieval_results=s.RETRIEVAL_MAX_RESULTS,
        max_concurrent_tasks=s.USER_MAX_CONCURRENT_TASKS,
    )


_budget_var: contextvars.ContextVar[ExecutionBudget | None] = contextvars.ContextVar(
    "execution_budget", default=None
)


def start_budget() -> tuple[ExecutionBudget, contextvars.Token]:
    budget = build_budget()
    return budget, _budget_var.set(budget)


def current_budget() -> ExecutionBudget | None:
    return _budget_var.get()


def reset_budget(token: contextvars.Token | None) -> None:
    if token is not None:
        try:
            _budget_var.reset(token)
        except ValueError:
            pass


def consume_tool_call(name: str) -> None:
    """工具执行前调用；无预算上下文时静默跳过（非对话链路）。"""
    budget = _budget_var.get()
    if budget is not None:
        budget.consume_tool(name)


def consume_agent_call(name: str = "") -> None:
    budget = _budget_var.get()
    if budget is not None:
        budget.consume_agent(name)


def note_tokens(tokens: int) -> None:
    budget = _budget_var.get()
    if budget is not None:
        budget.add_tokens(tokens)


def clamp_retrieval(requested: int | None) -> int:
    budget = _budget_var.get()
    if budget is None:
        return requested or get_settings().RETRIEVAL_MAX_RESULTS
    return budget.clamp_retrieval(requested)


# ── 用户级并发任务上限（Redis 跨进程） ────────────────────────────────────
_CONCURRENCY_TTL = 300


async def acquire_user_slot(redis, user_id: str) -> bool:
    """占用一个并发任务槽；返回 False 表示已达上限。

    Redis 异常时 fail-open（限流不能因为缓存故障把全部用户挡在门外）。
    """
    limit = get_settings().USER_MAX_CONCURRENT_TASKS
    key = f"concurrent:user:{user_id}"
    try:
        current = await redis.incr(key)
        if current == 1:
            await redis.expire(key, _CONCURRENCY_TTL)
        if current > limit:
            await redis.decr(key)
            return False
        return True
    except Exception as e:
        logger.warning(f"并发槽获取失败（fail-open）: {e}")
        return True


async def release_user_slot(redis, user_id: str) -> None:
    try:
        key = f"concurrent:user:{user_id}"
        if (await redis.decr(key)) < 0:
            await redis.set(key, 0)
    except Exception as e:
        logger.debug(f"并发槽释放失败（可忽略）: {e}")
