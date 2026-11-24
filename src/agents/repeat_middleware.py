# src/agents/repeat_middleware.py
#
# P0-1 打转检测的落地点：模型再提交"同一工具 + 同一组参数"时，不再执行工具，
# 直接回一条工具结果告诉模型"你已经查过了，换参数或换工具"。
#
# 为什么用 wrap_tool_call 而不是 after_model 改 AIMessage：
#   after_model 把 tool_call 从 AIMessage 里摘掉、再补一条 ToolMessage，会造成
#   "tool 消息没有对应的 tool_calls" —— 下一轮发给 OpenAI 兼容网关直接 400
#   （handoff 中间件为此专门写了 abefore_agent 清孤儿）。而 wrap_tool_call 保留
#   调用、只是不执行本体，协议天然配对，模型也能看到"被拦"的原因。
#
# 为什么按参数指纹判重：见 core/safety/repeat.py —— 工具名判重会把"连查三个不同症状"
# 这种正常排查误杀。

from __future__ import annotations

from typing import Any, Mapping

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, ToolMessage
from loguru import logger

from src.core.safety.repeat import DEFAULT_WINDOW, EXEMPT_TOOLS, describe, fingerprint


def is_repeat_call(
    messages: list[Any],
    call: Mapping[str, Any],
    *,
    window: int = DEFAULT_WINDOW,
) -> bool:
    """判断本次调用是否与最近 window 次调用里的某一次"同工具同参数"。

    历史 = 除最后一条 AIMessage（本次这批调用）之外所有 AIMessage 的工具调用，
    再加"同一条消息里和本次一模一样的并发调用"（第一个执行、后续算重复）。
    不靠 tool_call id 定位当前调用：实测同一 id 在多次工具轮里会被重复使用，
    按 id 断点会把历史整段截掉。
    """
    name = str(call.get("name") or "")
    if not name or name in EXEMPT_TOOLS:
        return False
    target = fingerprint(name, call.get("args") or {})

    ai_messages = [m for m in messages if isinstance(m, AIMessage)]
    history: list[str] = []
    for message in ai_messages[:-1]:
        for item in getattr(message, "tool_calls", None) or ():
            item_name = str(item.get("name") or "")
            if item_name in EXEMPT_TOOLS:
                continue
            history.append(fingerprint(item_name, item.get("args") or {}))

    call_id = str(call.get("id") or "")
    same_before_current = False
    matched_in_batch = False
    in_last_same = 0
    if ai_messages:
        for item in getattr(ai_messages[-1], "tool_calls", None) or ():
            item_name = str(item.get("name") or "")
            if item_name in EXEMPT_TOOLS:
                continue
            item_fp = fingerprint(item_name, item.get("args") or {})
            if item_fp == target:
                in_last_same += 1
            if str(item.get("id") or "") == call_id:
                matched_in_batch = True
                break
            if item_fp == target:
                same_before_current = True

    if same_before_current:
        return True  # 同一条消息里在本次之前已有一样的调用
    if not matched_in_batch and in_last_same >= 2:
        return True  # id 在轮间被复用、定位不到当前调用：两个一样就算重复
    return any(fp == target for fp in history[-window:])


class RepeatGuardMiddleware(AgentMiddleware):
    """打转拦截：重复的「工具+参数」调用不再执行，直接回提示给模型。"""

    name = "repeat_guard"

    async def awrap_tool_call(self, request: Any, handler: Any) -> Any:
        call = request.tool_call or {}
        state = getattr(request, "state", None)
        if isinstance(state, Mapping):
            messages = list(state.get("messages") or [])
        else:  # langgraph 传进来的是 AgentState(pydantic) 而不是 dict
            messages = list(getattr(state, "messages", None) or [])

        if is_repeat_call(messages, call):
            name = str(call.get("name") or "")
            logger.warning(f"[repeat_guard] 拦截重复调用 {name} args={call.get('args')}")
            return ToolMessage(
                content=describe(call, step=sum(
                    1 for m in messages if isinstance(m, AIMessage)
                    for _ in (getattr(m, "tool_calls", None) or ())
                )),
                tool_call_id=str(call.get("id") or ""),
                name=name,
            )

        return await handler(request)
