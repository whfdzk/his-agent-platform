# src/agents/direct_worker.py
#
# 单域直连：路由判定为"药品/医学知识"时，跳过 supervisor，直接把问题交给
# drug / knowledge agent，拿到答案就返回。
#
# 与 supervisor 路径的差别只有一个：不再有"再判断一次要不要换助手"的那次模型调用。
# 其余照旧 —— 长期记忆注入、知识依赖（Milvus/Neo4j）、追踪 span、用量回调、
# 超时与降级话术，全部与 worker_tools 里的工具调用保持一致，避免出现"直连更快
# 但少记了用量 / 少了记忆"的隐性回归。

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from loguru import logger

from src.core.concurrency import guarded
from src.core.context import UserContext
from src.core.langfuse_tracing import trace_span

SUPPORTED_AGENTS = ("drug", "knowledge")

# 直连时一个 worker 图最多走几步（一次模型↔工具往返≈2 步）
DIRECT_RECURSION_LIMIT = 8

_DEGRADE = {
    "drug": "用药咨询服务暂时繁忙，请稍后重试；用药安全相关疑问请咨询药师或医生。",
    "knowledge": "医学知识问答服务暂时繁忙，请稍后重试。",
}
_SPAN = {"drug": "drug_agent", "knowledge": "knowledge_agent"}


@dataclass
class _Runtime:
    """直连时给公共 helper 用的轻量 runtime：只需要 context / config / state / store。"""

    context: UserContext
    config: dict = field(default_factory=dict)
    state: dict = field(default_factory=dict)
    store: Any = None


async def _build_runtime(
    *, user_id: str, session_id: str, role: str, tenant_id: int,
    authenticated: bool, patient_id: int | None, thread_id: str,
) -> _Runtime:
    # 长期记忆 store：Milvus 不可用时返回 None，记忆注入自动跳过
    try:
        from src.agents.supervisor_agent import _get_long_term_store

        store = await _get_long_term_store()
    except Exception as e:  # 记忆增强失败不能拖垮直连
        logger.warning(f"直连取长期记忆 store 失败，跳过记忆: {type(e).__name__}: {e}")
        store = None
    return _Runtime(
        context=UserContext(
            user_id=user_id, session_id=session_id, authenticated=authenticated,
            patient_id=patient_id, tenant_id=tenant_id, role=role,
        ),
        config={"configurable": {"thread_id": thread_id}},
        state={},
        store=store,
    )


async def _prepare_message(runtime: _Runtime, message: str) -> str:
    """长期记忆注入，与 worker_tools._invoke_knowledge_worker 同口径。"""
    from src.agents.worker_tools import _with_long_term_memory

    return await _with_long_term_memory(runtime, message)


def _agent_of(code: str):
    from src.agents.workers.drug_agent import get_drug_agent
    from src.agents.workers.knowledge_agent import get_knowledge_agent

    return get_drug_agent() if code == "drug" else get_knowledge_agent()


def _sanitize(reply: str) -> str:
    """挡掉"工具调用 JSON 被当正文吐出来"的脏输出。

    模型在被收口（不给工具）时偶尔会输出 `{"tool_call": {"name": ...}}` 这种片段，
    直接回给用户等于把内部协议抖出来了；这种情况按降级话术处理更体面。
    """
    text = (reply or "").strip()
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text[:4].lower() == "json":
            text = text[4:].strip()
    if '"tool_call"' in text or text.startswith('{"name":'):
        logger.warning("[router] 直连输出疑似工具调用 JSON，按降级处理")
        return ""
    return text


async def run_direct_worker(
    code: str,
    message: str,
    *,
    user_id: str,
    session_id: str,
    role: str,
    tenant_id: int,
    authenticated: bool,
    patient_id: int | None,
    thread_id: str,
    handler: Any = None,
) -> str:
    """直连执行一个 worker，返回最终答案文本（失败返回降级话术）。"""
    if code not in SUPPORTED_AGENTS:
        raise ValueError(f"不支持直连的助手: {code}")

    from src.core.config import get_settings
    from src.knowledge.deps import bind_knowledge_deps, build_knowledge_deps

    settings = get_settings()
    runtime = await _build_runtime(
        user_id=user_id, session_id=session_id, role=role, tenant_id=tenant_id,
        authenticated=authenticated, patient_id=patient_id, thread_id=thread_id,
    )
    prepared = await _prepare_message(runtime, message)

    try:
        async with trace_span(
            _SPAN[code], "agent", agent_name=code,
            attributes={"message_chars": len(prepared), "route": "direct"},
        ):
            deps = await build_knowledge_deps(
                db_session=None, user_id=user_id, role=role,
            )
            with bind_knowledge_deps(deps):
                agent = _agent_of(code)
                result = await guarded(
                    agent.ainvoke(
                        {"messages": [{"role": "user", "content": prepared}]},
                        config={
                            # 直连没有 supervisor 兜底，模型自己的"模型↔工具"往返上限就是
                            # 整个请求的耗时上限：8 步≈3 次模型调用，超了当降级处理，
                            # 不要让它一路滚到 150s 被网关判超时 500。
                            "recursion_limit": DIRECT_RECURSION_LIMIT,
                            **({"callbacks": [handler]} if handler else {}),
                        },
                    ),
                    timeout=getattr(settings, "WORKER_TIMEOUT", 60.0),
                )
        reply = result["messages"][-1].content
        reply = _sanitize(reply if isinstance(reply, str) else str(reply))
        return reply or _DEGRADE[code]
    except Exception as e:
        logger.warning(f"{code} 直连降级: {type(e).__name__}: {e}")
        return _DEGRADE[code]


async def stream_direct_worker(
    code: str,
    message: str,
    *,
    user_id: str,
    session_id: str,
    role: str,
    tenant_id: int,
    authenticated: bool,
    patient_id: int | None,
    thread_id: str,
    handler: Any = None,
) -> AsyncIterator[Any]:
    """直连流式：逐 chunk 产出 (AIMessageChunk, metadata)，由上层提出 token。"""
    if code not in SUPPORTED_AGENTS:
        raise ValueError(f"不支持直连的助手: {code}")

    from src.knowledge.deps import bind_knowledge_deps, build_knowledge_deps

    runtime = await _build_runtime(
        user_id=user_id, session_id=session_id, role=role, tenant_id=tenant_id,
        authenticated=authenticated, patient_id=patient_id, thread_id=thread_id,
    )
    prepared = await _prepare_message(runtime, message)

    deps = await build_knowledge_deps(
        db_session=None, user_id=user_id, role=role,
    )
    with bind_knowledge_deps(deps):
        agent = _agent_of(code)
        async with trace_span(
            _SPAN[code], "agent", agent_name=code,
            attributes={"message_chars": len(prepared), "route": "direct_stream"},
        ):
            async for chunk in agent.astream(
                {"messages": [{"role": "user", "content": prepared}]},
                config={
                    # 与非流式直连同口径：不让"自己再查一次"的循环一路滚下去
                    "recursion_limit": DIRECT_RECURSION_LIMIT,
                    **({"callbacks": [handler]} if handler else {}),
                },
                stream_mode="messages",
            ):
                yield chunk
