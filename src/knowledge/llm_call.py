# src/knowledge/llm_call.py
#
# 知识链路内部 LLM 调用的统一入口，解决两个问题：
#
# 1) 超时。工具内部的抽取/改写/生成 SQL/核查这些调用原先既不限时也不限长，
#    上游一变慢就无限等待，最终只能撞外层 TURN_TIMEOUT（150s）整轮降级。
#
# 2) 用量记账。token_usage 的 handler 是挂在最外层 agent.ainvoke 的
#    config={"callbacks": [...]} 上，靠 LangChain 的 contextvar 向子 runnable 传播。
#    工具里直接 llm.ainvoke(...) 不显式继承时，这次调用可能脱离回调链，
#    耗时会从用量表里消失（r11 那次 38.9s 的融合生成就查不到）。
#    这里统一用 ensure_config() 把当前 config 带进去。
#
# 注意：超时用 asyncio.wait_for 只取消协程，不保证终止线程池里的底层调用；
# 对 HTTP 类 LLM 调用没有副作用，对任何阻塞型调用也只是"尽快返回"。

from __future__ import annotations

import asyncio
from typing import Any

from langchain_core.messages import BaseMessage
from langchain_core.runnables.config import ensure_config
from loguru import logger

# 工具内"取数/抽取/生成语句/核查"这类调用：超过这个时长基本等于上游异常
UTIL_TIMEOUT = 10.0
# 工具内"写整段回答"的调用：输出长，给多一点，但不允许无限等
ANSWER_TIMEOUT = 30.0


async def call_llm(
    llm: Any,
    messages: list[BaseMessage],
    *,
    timeout: float = UTIL_TIMEOUT,
    label: str = "",
    default: Any = None,
) -> Any:
    """带超时与回调继承的 LLM 调用；超时/异常返回 default（由调用方降级）。

    llm 也可以是 bind 过的 runnable（如 llm.bind(max_tokens=800)），
    只要它暴露 ainvoke(messages, config=...) 即可。
    """
    cfg = ensure_config()
    name = label or "知识工具内 LLM 调用"
    try:
        return await asyncio.wait_for(
            llm.ainvoke(messages, config=cfg), timeout=timeout,
        )
    except asyncio.TimeoutError:
        logger.warning(f"{name} 超时（{timeout}s），按降级处理")
        return default
    except Exception as e:
        logger.warning(f"{name} 失败（{type(e).__name__}）: {e}")
        return default


async def call_llm_text(
    llm: Any,
    messages: list[BaseMessage],
    *,
    timeout: float = UTIL_TIMEOUT,
    label: str = "",
    default: str = "",
) -> str:
    """call_llm 的文本版：拿不到内容时返回 default，调用方不用再判 None。"""
    resp = await call_llm(llm, messages, timeout=timeout, label=label, default=None)
    if resp is None:
        return default
    content = getattr(resp, "content", "") or ""
    return content.strip() or default
