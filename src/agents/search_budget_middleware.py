# src/agents/search_budget_middleware.py
#
# 检索收敛：worker 自己的"再查一个来源"也要有上限。
#
# 背景（实测）：把 supervisor 摘掉、单域直连之后，"阿司匹林有什么副作用"
# 从 150s 500 降到 27s，但 token_usage 显示模型仍在循环——
# query_drug_info 返回"未收录"后它换 search_knowledge_graph 再查，
# 图谱也空就再换一次，直到 recursion_limit 撞墙降级。
# 打转拦截（RepeatGuardMiddleware）拦的是"同工具同参数"，换工具换关键词它管不住。
#
# 所以这里用两条确定性规则收口：
# 1. 本轮工具调用累计到 3 次 → 下一次模型调用不给工具，逼它基于已有信息作答；
# 2. 连续两条工具结果都是"未收录/未检索到/不可用" → 同上处理。
# 模型拿到的是"不许再查"的系统提示，而不是悄悄丢掉工具（悄悄丢会让它硬编答案）。

from __future__ import annotations

import dataclasses
from typing import Any, Callable

from langchain.agents.middleware import AgentMiddleware, ModelRequest
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from loguru import logger

MAX_TOOL_CALLS = 3
EMPTY_MARKERS = (
    "暂未收录", "未收录", "未检索到", "未找到", "暂时不可用", "查询终结",
)

FORCE_ANSWER_HINT = """
## 本轮检索到此为止
上面的工具结果就是全部可用证据，本轮不再检索。现在直接作答：
- 查到的部分给结论，并标注来源（文档名/图谱关系）；
- 没查到的部分明确写"平台知识库暂未收录"，不要凭记忆补全具体条目；
- 结尾给一句可执行建议（查看包装说明书 / 咨询药师或医生）。
"""


def _tool_calls_so_far(messages: list[Any]) -> int:
    return sum(
        len(getattr(m, "tool_calls", None) or ())
        for m in messages
        if isinstance(m, AIMessage)
    )


def _is_empty_result(message: Any) -> bool:
    if not isinstance(message, ToolMessage):
        return False
    content = message.content if isinstance(message.content, str) else str(message.content or "")
    return any(marker in content for marker in EMPTY_MARKERS)


def _consecutive_empties(messages: list[Any]) -> int:
    """末尾连续几条工具结果都是空检索（工具调用消息穿插在中间不算打断）。"""
    count = 0
    for m in reversed(messages):
        if isinstance(m, ToolMessage):
            if _is_empty_result(m):
                count += 1
                continue
            break
        if isinstance(m, AIMessage):
            continue
        break
    return count


class SearchBudgetMiddleware(AgentMiddleware):
    """把"再查一次"收敛成"现在作答"。"""

    name = "search_budget"

    def __init__(self, *, max_tool_calls: int = MAX_TOOL_CALLS):
        self._max_tool_calls = max_tool_calls

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Any],
    ) -> Any:
        messages = list(request.messages or [])
        used = _tool_calls_so_far(messages)
        empties = _consecutive_empties(messages)

        if used < self._max_tool_calls and empties < 2:
            return await handler(request)

        logger.info(f"[search_budget] 收口作答：工具调用 {used} 次，连续空结果 {empties} 条")
        base = ""
        if request.system_message is not None and isinstance(request.system_message.content, str):
            base = request.system_message.content
        return await handler(
            dataclasses.replace(
                request,
                tools=[],
                # tool_choice="none" 明确告诉网关"这次不要出工具调用"，否则个别模型
                # 会把本该是函数调用的 JSON 当正文吐出来（实测泄漏 `{"tool_call":...}`）
                tool_choice="none",
                system_message=SystemMessage(content=f"{base}\n{FORCE_ANSWER_HINT}"),
            )
        )
