# src/core/llm_callbacks.py
#
# Token 用量 / 调用耗时 / 全链路追踪回调。
#
# 设计要点：
# - 每次用户请求创建一个 handler 实例（make_usage_handler），挂在最外层
#   agent.ainvoke / graph.ainvoke 的 config={"callbacks": [handler]} 上。
# - LangChain 回调会自动向子 runnable 传播，因此无需在每个 worker 内重复挂载
#   （重复挂载会导致同一次 LLM 调用被记两行）。
# - worker 归因：handler 内部维护 run tree（run_id -> parent_run_id），
#   LLM 调用发生时沿 parent 链向上找最近的 tool run（如 call_drug_agent），
#   找到则归因到该 worker，否则归因到 default_worker（supervisor / inquiry）。
# - 追踪：每次 LLM / Tool 调用都会在当前 trace 上开一个 span，
#   记录模型名与版本、Token、耗时、错误类型；最终由 TraceRecorder 统一落库。

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

from langchain_core.callbacks import AsyncCallbackHandler
from loguru import logger

from src.core.limits import note_tokens
from src.core.persist_queue import PersistItem, enqueue
from src.core.tracing import current_trace

# 工具名 -> worker 名映射（未命中的工具直接用工具名）
TOOL_WORKER_MAP = {
    "call_inquiry_agent": "inquiry",
    "call_drug_agent": "drug",
    "call_knowledge_agent": "knowledge",
    "call_report_agent": "report",
    "call_operation_agent": "operation",
    "save_memory": "memory",
    "search_memory": "memory",
}


class UsageTrackingHandler(AsyncCallbackHandler):
    """记录每次 LLM / Tool 调用的模型、Token、耗时，并归因到 worker。"""

    def __init__(
        self,
        user_id: str = "",
        session_id: str = "",
        turn_id: str = "",
        default_worker: str = "supervisor",
    ):
        self.user_id = user_id
        self.session_id = session_id
        self.turn_id = turn_id
        self.default_worker = default_worker
        # run_id -> (model, start_monotonic, parent_run_id)
        self._llm_starts: dict[Any, tuple[str, float, Any]] = {}
        # run_id -> parent_run_id（所有 run，用于向上回溯）
        self._parents: dict[Any, Any] = {}
        # tool run_id -> worker 名
        self._tool_runs: dict[Any, str] = {}
        # run_id -> 追踪 span 句柄（LLM / Tool）
        self._spans: dict[Any, Any] = {}
        # 在途落库任务引用（防 GC）
        self._pending_tasks: set[asyncio.Task] = set()

    # ── run tree 归因 ────────────────────────────────────────────────────
    def _resolve_worker(self, parent_run_id: Any) -> str:
        cur = parent_run_id
        seen: set[Any] = set()
        while cur is not None and cur not in seen:
            seen.add(cur)
            worker = self._tool_runs.get(cur)
            if worker:
                return worker
            cur = self._parents.get(cur)
        return self.default_worker

    # ── 回调事件 ────────────────────────────────────────────────────────
    async def on_tool_start(
        self, serialized: dict[str, Any], tool: Any, *, run_id: Any = None,
        parent_run_id: Any = None, input_str: str | None = None, **kwargs: Any,
    ) -> None:
        name = ""
        if isinstance(serialized, dict):
            name = serialized.get("name") or ""
        if not name:
            name = getattr(tool, "name", "") or ""
        worker = TOOL_WORKER_MAP.get(name, name or "tool")
        self._parents[run_id] = parent_run_id
        self._tool_runs[run_id] = worker

        trace = current_trace()
        if trace is not None:
            trace.count_tool(name or worker)
            trace.count_agent(worker)
            self._spans[run_id] = trace.start_span(
                f"tool.{name or worker}", "tool",
                agent_name=worker, tool_name=name or worker,
                attributes={"parent_worker": self._resolve_worker(parent_run_id)},
            )

    async def on_tool_end(self, output: Any, *, run_id: Any = None, **kwargs: Any) -> None:
        trace = current_trace()
        if trace is not None:
            span = self._spans.pop(run_id, None)
            trace.end_span(span, status="ok")

    async def on_tool_error(self, error: BaseException, *, run_id: Any = None, **kwargs: Any) -> None:
        trace = current_trace()
        if trace is not None:
            span = self._spans.pop(run_id, None)
            trace.end_span(span, status="error", error_type=type(error).__name__)

    async def on_chat_model_start(
        self, serialized: dict[str, Any], messages: list, *, run_id: Any = None,
        parent_run_id: Any = None, **kwargs: Any,
    ) -> None:
        model = ""
        if isinstance(serialized, dict):
            kw = serialized.get("kwargs") or {}
            model = kw.get("model") or kw.get("model_name") or ""
        if not model:
            ip = kwargs.get("invocation_params") or {}
            model = ip.get("model") or ""
        self._parents[run_id] = parent_run_id
        self._llm_starts[run_id] = (str(model), time.monotonic(), parent_run_id)

        trace = current_trace()
        if trace is not None:
            worker = self._resolve_worker(parent_run_id)
            trace.count_agent(worker)
            self._spans[run_id] = trace.start_span(
                "llm.call", "llm",
                agent_name=worker, model_name=str(model) or None,
                attributes={"model_requested": str(model)},
            )

    async def on_llm_end(self, response: Any, *, run_id: Any = None, **kwargs: Any) -> None:
        info = self._llm_starts.pop(run_id, None)
        if info is None:
            return
        model, t0, parent_run_id = info
        latency_ms = int((time.monotonic() - t0) * 1000)
        # 故障转移后实际服务的模型与 on_chat_model_start 里记录的未必是同一个
        # （回调拿到的是序列化后的主模型名），这里改用响应里回传的真实模型名。
        model = _actual_model(response) or model
        model_version = _model_version(response)

        prompt_tokens = completion_tokens = 0
        try:
            gen = response.generations[0][0] if response.generations else None
            meta = getattr(gen, "usage_metadata", None) or {}
            prompt_tokens = int(meta.get("input_tokens") or 0)
            completion_tokens = int(meta.get("output_tokens") or 0)
            if not prompt_tokens:
                # 兼容未填充 usage_metadata 的实现：退回 response_metadata.token_usage
                tu = (getattr(gen, "response_metadata", None) or {}).get("token_usage") or {}
                prompt_tokens = int(tu.get("prompt_tokens") or 0)
                completion_tokens = int(tu.get("completion_tokens") or 0)
        except Exception:
            pass

        total = prompt_tokens + completion_tokens
        note_tokens(total)

        trace = current_trace()
        if trace is not None:
            trace.add_tokens(prompt_tokens, completion_tokens, model, model_version)
            span = self._spans.pop(run_id, None)
            trace.end_span(
                span, status="ok", prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                attributes={"model_actual": model, "model_version": model_version,
                            "latency_ms": latency_ms},
            )

        worker = self._resolve_worker(parent_run_id)
        # 持有任务引用（避免被 GC 提前回收），完成后自动清理
        task = asyncio.create_task(self._persist(
            worker=worker, model=model,
            prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
            latency_ms=latency_ms,
        ))
        self._pending_tasks.add(task)
        task.add_done_callback(self._pending_tasks.discard)

    async def on_llm_error(self, error: BaseException, *, run_id: Any = None, **kwargs: Any) -> None:
        self._llm_starts.pop(run_id, None)
        trace = current_trace()
        if trace is not None:
            span = self._spans.pop(run_id, None)
            trace.end_span(span, status="error", error_type=type(error).__name__)

    # ── 落库：投递到全局批量持久化队列（不再每次调用新建 DB session） ──────
    async def _persist(
        self, worker: str, model: str,
        prompt_tokens: int, completion_tokens: int, latency_ms: int,
    ) -> None:
        try:
            enqueue(PersistItem(kind="token_usage", payload={
                "user_id": self.user_id,
                "session_id": self.session_id,
                "turn_id": self.turn_id,
                "worker": worker,
                "model": model,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
                "latency_ms": latency_ms,
            }))
        except Exception as e:
            logger.warning(f"token_usage 入队失败（不影响主流程）: {e}")


def _actual_model(response: Any) -> str:
    """从 LLMResult 里取真正服务的模型名（各家字段名不统一，逐个兜底）。"""
    try:
        llm_output = getattr(response, "llm_output", None) or {}
        name = llm_output.get("model_name")
        if name:
            return str(name)
        gen = response.generations[0][0] if response.generations else None
        if gen is None:
            return ""
        for source in (
            getattr(gen, "response_metadata", None) or {},
            getattr(gen, "generation_info", None) or {},
        ):
            name = source.get("model_name") or source.get("model")
            if name:
                return str(name)
    except Exception:
        pass
    return ""


def _model_version(response: Any) -> str:
    """模型版本 / 指纹：OpenAI 兼容网关一般回传 system_fingerprint。"""
    try:
        gen = response.generations[0][0] if response.generations else None
        meta = getattr(gen, "response_metadata", None) or {}
        for key in ("system_fingerprint", "model_version", "model_revision"):
            if meta.get(key):
                return str(meta[key])[:100]
    except Exception:
        pass
    return ""


def make_usage_handler(
    user_id: str,
    session_id: str,
    turn_id: str | None = None,
    default_worker: str = "supervisor",
) -> UsageTrackingHandler:
    """创建一次请求级的用量追踪 handler。turn_id 缺省时自动生成。"""
    return UsageTrackingHandler(
        user_id=user_id,
        session_id=session_id,
        turn_id=turn_id or uuid.uuid4().hex,
        default_worker=default_worker,
    )
