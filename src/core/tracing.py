# src/core/tracing.py
#
# Agent 全链路追踪。
#
# 覆盖链路：
#   API 请求 → Supervisor → Consultation Agent → Retrieval(Milvus/Neo4j)
#   → LLM → Safety Engine → Final Response
#
# 每次执行至少记录：request_id / trace_id / user_id（受控访问）、Agent 名称与版本、
# Prompt 版本、模型名称与版本、Tool 调用、检索来源、耗时、Token、错误类型、安全审核结果。
#
# 设计约束：
# - 只存结构化字段与脱敏摘要，绝不写完整病历 / 身份证 / 手机号 / 报告原文
# - 落库走批量队列（与 chat_message / token_usage 一致），不阻塞主链路
# - contextvars 贯穿 asyncio 任务，SSE 生成器内创建的 task 会自动继承

from __future__ import annotations

import asyncio
import contextvars
import random
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from src.core.concurrency import request_id_var
from src.core.persist_queue import PersistItem, enqueue
from src.core.redaction import redact_payload, redact_text
from src.core.versions import agent_meta

# 允许的 span 类型
SPAN_KINDS = frozenset({
    "api", "agent", "tool", "llm", "retrieval", "safety", "final_response", "persistence",
})


@dataclass
class SpanHandle:
    """一个进行中的 span：由 trace_span 上下文管理器创建并关闭。"""

    span_id: str
    name: str
    kind: str
    parent_span_id: str | None
    started_at: float
    attributes: dict[str, Any] = field(default_factory=dict)


class TraceRecorder:
    """一轮执行的追踪记录器（内存聚合，结束时一次性落库）。"""

    def __init__(
        self,
        *,
        trace_id: str | None = None,
        request_id: str = "",
        user_id: str = "",
        session_id: str = "",
        turn_id: str = "",
        entrypoint: str = "",
        agent_name: str = "supervisor",
        sampled: bool = True,
    ) -> None:
        meta = agent_meta(agent_name)
        # 采样关闭时不记录 span、不落库：链路仍然可跑，只是没有轨迹
        self.sampled = sampled
        self.trace_id = trace_id or uuid.uuid4().hex[:32]
        self.request_id = request_id or request_id_var.get("")
        self.user_id = user_id
        self.session_id = session_id
        self.turn_id = turn_id
        self.entrypoint = entrypoint
        self.agent_name = agent_name
        self.agent_version = meta["version"]
        self.prompt_version = meta["prompt_version"]
        self.started_at = time.monotonic()
        self.status = "running"
        self.error_type: str | None = None
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.tool_calls = 0
        self.agent_calls = 0
        self.tool_names: list[str] = []
        self.retrieval_sources: list[dict] = []
        self.retrieval_count = 0
        # 引用核查结论（None=本轮未核查）：写在这里而不是回传调用方，
        # 是为了让审计落库能直接读到，不必给每个检索通道加一个返回值
        self.grounded: bool | None = None
        self.safety_level = "info"
        self.safety_result = "allow"
        self.safety_rules: list[str] = []
        self.model_names: list[str] = []
        self.model_versions: list[str] = []
        self.input_chars = 0
        self.output_chars = 0
        self.spans: list[dict] = []
        self._open: dict[str, SpanHandle] = {}
        self._finished = False
        self._root_span: SpanHandle | None = None

    # ── span 管理 ────────────────────────────────────────────────────────
    def start_span(
        self,
        name: str,
        kind: str = "agent",
        *,
        agent_name: str | None = None,
        tool_name: str | None = None,
        model_name: str | None = None,
        model_version: str | None = None,
        parent_span_id: str | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> SpanHandle:
        if not self.sampled:
            return None
        kind = kind if kind in SPAN_KINDS else "agent"
        span = SpanHandle(
            span_id=uuid.uuid4().hex[:16],
            name=name[:100],
            kind=kind,
            parent_span_id=parent_span_id,
            started_at=time.monotonic(),
            attributes=redact_payload(attributes or {}),
        )
        span.attributes.setdefault("agent_name", agent_name or self.agent_name)
        if tool_name:
            span.attributes["tool_name"] = tool_name
        if model_name:
            span.attributes["model_name"] = model_name
        if model_version:
            span.attributes["model_version"] = model_version
        self._open[span.span_id] = span
        return span

    def end_span(
        self,
        span: SpanHandle | None,
        *,
        status: str = "ok",
        error_type: str | None = None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        attributes: dict[str, Any] | None = None,
        agent_name: str | None = None,
    ) -> None:
        if span is None:
            return
        self._open.pop(span.span_id, None)
        latency_ms = int((time.monotonic() - span.started_at) * 1000)
        attrs = {**span.attributes, **(attributes or {})}
        if agent_name:
            attrs["agent_name"] = agent_name
        # Prompt 版本跟着 Agent 走：span 上也留一份，便于定位是哪版 Prompt 引发的行为
        resolved_agent = str(attrs.get("agent_name") or "")
        if resolved_agent and "prompt_version" not in attrs:
            attrs["prompt_version"] = agent_meta(resolved_agent).get("prompt_version", "")
        self.spans.append({
            "trace_id": self.trace_id,
            "span_id": span.span_id,
            "parent_span_id": span.parent_span_id,
            "name": span.name,
            "kind": span.kind,
            "agent_name": attrs.get("agent_name") or "",
            "agent_version": agent_meta(str(attrs.get("agent_name") or "")).get("version", ""),
            "tool_name": attrs.get("tool_name") or None,
            "model_name": attrs.get("model_name") or None,
            "model_version": attrs.get("model_version") or None,
            "status": status,
            "error_type": error_type,
            "latency_ms": latency_ms,
            "prompt_tokens": int(prompt_tokens or 0),
            "completion_tokens": int(completion_tokens or 0),
            "total_tokens": int(prompt_tokens or 0) + int(completion_tokens or 0),
            "attributes": redact_payload(attrs),
        })

    # ── 计数器 ──────────────────────────────────────────────────────────
    def add_tokens(
        self, prompt_tokens: int, completion_tokens: int,
        model_name: str = "", model_version: str = "",
    ) -> None:
        self.prompt_tokens += int(prompt_tokens or 0)
        self.completion_tokens += int(completion_tokens or 0)
        if model_name and model_name not in self.model_names:
            self.model_names.append(model_name[:100])
        if model_version and model_version not in self.model_versions:
            self.model_versions.append(model_version[:100])

    def count_tool(self, name: str) -> None:
        self.tool_calls += 1
        if name and name not in self.tool_names:
            self.tool_names.append(name[:64])

    def count_agent(self, name: str = "") -> None:
        self.agent_calls += 1

    def add_retrieval(self, engine: str, items: list[dict], top_k: int = 0) -> None:
        """记录一次检索：保留引用定位（文档/页码/片段序号/摘句）与分数，不保留整段正文。

        这些字段同时是 citation（回答引用）的数据来源：回答正文里的"依据哪份资料
        第几页"必须可回溯，否则无法核对模型有没有编造出处。
        """
        safe_items = []
        for item in items[:20]:
            safe_items.append({
                "engine": engine,
                "source": redact_text(str(item.get("source") or item.get("doc_name") or ""), 80),
                "doc_id": redact_text(str(item.get("doc_id") or item.get("id") or ""), 64),
                "doc_name": redact_text(str(item.get("doc_name") or item.get("title") or ""), 120),
                "doc_type": str(item.get("doc_type") or "")[:40],
                "page_number": item.get("page_number"),
                "chunk_index": item.get("chunk_index"),
                "score": item.get("score"),
                # 分数语义：vector=余弦相似度 / rerank=精排分 / rrf=融合排名分 / bm25=关键词分。
                # 量纲不同（实测出现过 21.66 与 0.56 并存），不标类型就没法按分数排序或设阈值
                "score_type": str(item.get("score_type") or "vector")[:16],
                "title": redact_text(str(item.get("title") or item.get("doc_name") or ""), 80),
                # 摘句只留 120 字且过 PHI 正则：够定位原文，又不会把整段病历带进库
                "quote": redact_text(str(item.get("text") or ""), 120),
            })
        self.retrieval_sources.extend(safe_items)
        self.retrieval_count += len(safe_items or [])
        span = self.start_span(
            f"retrieval.{engine}", "retrieval",
            attributes={"engine": engine, "returned": len(safe_items), "top_k": top_k},
        )
        self.end_span(span, status="ok")

    def set_safety(self, level: str, action: str, rules: list[str]) -> None:
        order = {"info": 0, "warn": 1, "high": 2, "critical": 3}
        if order.get(level, 0) >= order.get(self.safety_level, 0):
            self.safety_level = level
            self.safety_result = action
        for r in rules:
            if r not in self.safety_rules:
                self.safety_rules.append(r)

    # ── 结束并落库 ──────────────────────────────────────────────────────
    def finish(self, status: str = "success", error_type: str | None = None) -> None:
        if self._finished:
            return
        self._finished = True
        self.status = status
        self.error_type = error_type
        if not self.sampled:
            # 未采样：不落库，直接结束
            return
        # 关闭遗留 span（异常路径可能没有正常退出上下文）
        for span in list(self._open.values()):
            self.end_span(span, status="error" if status == "error" else "ok")

        latency_ms = int((time.monotonic() - self.started_at) * 1000)
        payload = {
            "trace_id": self.trace_id,
            "request_id": self.request_id,
            "user_id": self.user_id,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "entrypoint": self.entrypoint,
            "agent_name": self.agent_name,
            "agent_version": self.agent_version,
            "prompt_version": self.prompt_version,
            "model_name": (self.model_names[0] if self.model_names else ""),
            "model_version": (self.model_versions[0] if self.model_versions else ""),
            "status": status,
            "error_type": error_type,
            "latency_ms": latency_ms,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.prompt_tokens + self.completion_tokens,
            "tool_calls": self.tool_calls,
            "tool_names": self.tool_names,
            "agent_calls": self.agent_calls,
            "retrieval_count": self.retrieval_count,
            "retrieval_sources": self.retrieval_sources,
            "safety_level": self.safety_level,
            "safety_result": self.safety_result,
            "safety_rules": self.safety_rules,
            "input_chars": self.input_chars,
            "output_chars": self.output_chars,
        }
        _enqueue_trace(payload)
        for span in self.spans:
            _enqueue_span(span)

    def add_span_snapshot(self, name: str, kind: str, latency_ms: int, **attributes: Any) -> None:
        """记录一个已完成的瞬时节点（如 Safety Engine 审核）。"""
        span = self.start_span(name, kind, attributes=attributes)
        self.end_span(span, status="ok")


def _enqueue_trace(payload: dict) -> None:
    try:
        enqueue(PersistItem(kind="agent_trace", payload=payload))
    except Exception as e:  # 追踪失败绝不能影响主流程
        logger.warning(f"agent_trace 入队失败: {e}")


def _enqueue_span(payload: dict) -> None:
    try:
        enqueue(PersistItem(kind="agent_span", payload=payload))
    except Exception as e:
        logger.warning(f"agent_span 入队失败: {e}")


# ── contextvars 贯穿 ──────────────────────────────────────────────────────
_trace_var: contextvars.ContextVar[TraceRecorder | None] = contextvars.ContextVar(
    "current_trace", default=None
)


def _should_sample() -> bool:
    """按 TRACE_SAMPLE_RATE 采样：>=1 全采，<=0 全不采，中间按比例。"""
    from src.core.config import get_settings

    try:
        rate = float(get_settings().TRACE_SAMPLE_RATE)
    except Exception:
        return True
    if rate >= 1.0:
        return True
    if rate <= 0.0:
        return False
    return random.random() < rate


def start_trace(**kwargs: Any) -> tuple[TraceRecorder, contextvars.Token]:
    kwargs.setdefault("sampled", _should_sample())
    recorder = TraceRecorder(**kwargs)
    token = _trace_var.set(recorder)
    return recorder, token


def current_trace() -> TraceRecorder | None:
    return _trace_var.get()


def get_trace_id() -> str:
    rec = _trace_var.get()
    return rec.trace_id if rec else ""


def end_trace(
    recorder: TraceRecorder | None = None,
    token: contextvars.Token | None = None,
    *,
    status: str = "success",
    error_type: str | None = None,
) -> None:
    rec = recorder or _trace_var.get()
    if rec is not None:
        rec.finish(status=status, error_type=error_type)
    if token is not None:
        try:
            _trace_var.reset(token)
        except ValueError:
            # token 已在其它上下文重置
            pass


@asynccontextmanager
async def trace_span(
    name: str,
    kind: str = "agent",
    *,
    agent_name: str | None = None,
    tool_name: str | None = None,
    model_name: str | None = None,
    attributes: dict[str, Any] | None = None,
) -> AsyncIterator[SpanHandle | None]:
    """追踪一个异步阶段；异常会自动标记 error 并记录异常类型。"""
    rec = current_trace()
    if rec is None:
        yield None
        return
    span = rec.start_span(
        name, kind, agent_name=agent_name, tool_name=tool_name,
        model_name=model_name, attributes=attributes,
    )
    try:
        yield span
    except BaseException as e:  # 包括 TimeoutError / CancelledError
        rec.end_span(
            span,
            status="timeout" if isinstance(e, (TimeoutError, asyncio.CancelledError)) else "error",
            error_type=type(e).__name__,
        )
        raise
    else:
        rec.end_span(span, status="ok")


def bind_span(span: SpanHandle | None, key: str, value: Any) -> None:
    """给进行中的 span 补充属性（值会脱敏）。"""
    if span is None:
        return
    span.attributes[key] = redact_payload(value)


def log_context() -> dict[str, str]:
    """返回可直接 bind 到 loguru 的上下文字典。"""
    rec = current_trace()
    return {
        "request_id": request_id_var.get(""),
        "trace_id": rec.trace_id if rec else "",
        "session_id": rec.session_id if rec else "",
        "turn_id": rec.turn_id if rec else "",
    }
