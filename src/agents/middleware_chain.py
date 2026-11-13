"""
Agent 中间件链：可插拔的执行前后钩子。
借鉴天宫医疗的设计，把日志、监控、安全检查做成可插拔的中间件。
"""
from __future__ import annotations
from typing import Callable, Awaitable, Any
from dataclasses import dataclass
from loguru import logger
import time


@dataclass
class AgentContext:
    """Agent 执行上下文，在中间件间传递。"""
    agent_name: str
    user_input: str
    session_id: str = ""
    user_id: str = ""
    metadata: dict = None

    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}


class AgentMiddleware:
    """中间件基类，子类实现 before/after 钩子。"""

    async def before(self, ctx: AgentContext) -> None:
        """Agent 执行前调用，可修改 ctx 或抛出异常中断执行。"""
        pass

    async def after(self, ctx: AgentContext, result: Any, error: Exception | None = None) -> None:
        """Agent 执行后调用，无论成功失败都会调用。"""
        pass


class LoggingMiddleware(AgentMiddleware):
    """日志中间件：记录 Agent 调用和耗时。"""

    async def before(self, ctx: AgentContext) -> None:
        ctx.metadata["start_time"] = time.perf_counter()
        logger.info(f"[Agent:{ctx.agent_name}] 开始执行, input={ctx.user_input[:100]}...")

    async def after(self, ctx: AgentContext, result: Any, error: Exception | None = None) -> None:
        start = ctx.metadata.get("start_time", time.perf_counter())
        elapsed = (time.perf_counter() - start) * 1000
        if error:
            logger.error(f"[Agent:{ctx.agent_name}] 执行失败, 耗时={elapsed:.2f}ms, error={error}")
        else:
            logger.info(f"[Agent:{ctx.agent_name}] 执行成功, 耗时={elapsed:.2f}ms")


class MetricsMiddleware(AgentMiddleware):
    """监控中间件：记录 Agent 调用指标。"""

    async def before(self, ctx: AgentContext) -> None:
        pass

    async def after(self, ctx: AgentContext, result: Any, error: Exception | None = None) -> None:
        try:
            from src.api.routers.metrics import AGENT_TASKS
            status = "error" if error else "success"
            AGENT_TASKS.labels(agent=ctx.agent_name, status=status).inc()
        except ImportError:
            pass  # prometheus_client 未安装时静默跳过


class SafetyMiddleware(AgentMiddleware):
    """安全中间件：检查输入是否包含敏感词。"""

    SENSITIVE_WORDS = ["自杀", "自残", "杀人", "炸弹", "毒品"]

    async def before(self, ctx: AgentContext) -> None:
        for word in self.SENSITIVE_WORDS:
            if word in ctx.user_input:
                logger.warning(f"[Agent:{ctx.agent_name}] 检测到敏感词: {word}")
                ctx.metadata["blocked"] = True
                raise ValueError(f"输入包含敏感词，已拦截")


class MiddlewareChain:
    """中间件链：按顺序执行 before，逆序执行 after。"""

    def __init__(self):
        self.middlewares: list[AgentMiddleware] = []

    def add(self, middleware: AgentMiddleware) -> "MiddlewareChain":
        self.middlewares.append(middleware)
        return self

    async def run(self, ctx: AgentContext, handler: Callable[[AgentContext], Awaitable[Any]]) -> Any:
        """执行中间件链 + 实际 handler。"""
        # 执行所有 before
        for mw in self.middlewares:
            await mw.before(ctx)

        error = None
        result = None
        try:
            result = await handler(ctx)
            return result
        except Exception as e:
            error = e
            raise
        finally:
            # 逆序执行所有 after
            for mw in reversed(self.middlewares):
                try:
                    await mw.after(ctx, result, error)
                except Exception as e:
                    logger.warning(f"中间件 after 钩子执行失败: {e}")


# 默认中间件链
def get_default_middleware_chain() -> MiddlewareChain:
    chain = MiddlewareChain()
    chain.add(LoggingMiddleware())
    chain.add(MetricsMiddleware())
    chain.add(SafetyMiddleware())
    return chain
