# src/agents/tool_runtime_middleware.py
#
# 把 worker 的每次工具调用交给 harness/runtime 网关执行。
#
# 为什么用中间件而不是改每个工具：工具分散在 tools/ 与 MCP 两处，逐个包 try/except
# 既漏又难维护；awrap_tool_call 是 LangGraph ToolNode 的统一入口，一处收口就覆盖
# 全部工具（含 MCP）。
#
# 顺序：放在 RepeatGuard / SearchBudget / SkillCatalog 之后。打转拦截仍然先行——
# 重复调用直接回上次结论，不必再走一次真实执行；网关只负责真被执行的那部分。

from __future__ import annotations

from typing import Any, Callable

from langchain.agents.middleware import AgentMiddleware, ToolCallRequest
from langchain_core.messages import ToolMessage
from loguru import logger

from src.harness import runtime as harness_runtime


class ToolRuntimeMiddleware(AgentMiddleware):
    """受控执行：策略前置 → 超时 → 只读重试 → 异常归一成 Observation。"""

    name = "tool_runtime"

    def __init__(self, gateway: harness_runtime.ToolRuntimeGateway | None = None):
        self._gateway = gateway

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Any],
    ) -> Any:
        gateway = self._gateway or harness_runtime.get_gateway()
        call = getattr(request, "tool_call", None) or {}
        tool_name = str(call.get("name") or "")
        call_id = str(call.get("id") or "")
        args = call.get("args")

        # 带 interrupt 语义的工具不能被超时/异常归一包裹，否则挂起会被当成失败
        if tool_name in harness_runtime.SPECIAL_TOOLS:
            return await handler(request)

        observation = await gateway.execute(tool_name, args, lambda: handler(request))
        if observation.ok:
            return observation.result
        logger.info(f"[runtime] 工具 {tool_name} 未成功，回给模型结构化 Observation")
        return ToolMessage(
            content=observation.as_error_text(),
            tool_call_id=call_id,
            status="error",
        )
