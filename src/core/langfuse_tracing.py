"""全链路追踪（唯一实现，基于 Langfuse）。

历史上有过两套：自研 `src/core/tracing.py` 把 trace/span 落到 agent_traces/agent_spans
两张表，另有一层 Langfuse 薄壳。现已统一到本文件，数据库那两套表不再写入，
链路查询统一走 Langfuse UI（自托管 http://localhost:3000）。

对外 API 保持原来那套名字（start_trace / trace_span / current_trace …），
只是落点从「Postgres 行」换成「Langfuse observation」，业务侧只需改 import。

设计约束：
1. **没配 key 就彻底降级**：LANGFUSE_ENABLED 不为真 / key 缺失 / SDK 初始化失败，
   全部退化为 no-op，业务代码不需要判断今天有没有开观测。
2. **上报失败不许影响业务**：所有 SDK 调用包在 try 里，异常只写 warning。
3. 一个「轮次」= 一条 trace：session_id 用会话 id，user_id 用平台用户 id，
   这样 Langfuse 的 Sessions / Users 视图可直接用。
4. kind → Langfuse observation type 的映射集中在 `_AS_TYPE`，新增节点类型只改这里。

环境变量（由 src.core.config.Settings 读取）：
    LANGFUSE_ENABLED / LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY
    LANGFUSE_BASE_URL / LANGFUSE_ENVIRONMENT
"""
from __future__ import annotations

import contextvars
import logging
import os
import random
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from src.core.redaction import redact_payload, redact_text

logger = logging.getLogger(__name__)


class _OtelDetachNoise(logging.Filter):
    """屏蔽 OTel 的 "Failed to detach context"。

    FastAPI 的请求协程与 asyncio 任务之间 contextvars 不共享 Context 对象，
    langfuse SDK 退出 span 时 detach token 会报这一条 error；span 本身已经正常
    结束并上报（events_core 里有完整数据），这条只是噪音，按请求刷屏。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return "Failed to detach context" not in record.getMessage()


logging.getLogger("opentelemetry.context").addFilter(_OtelDetachNoise())

# 自研 kind → Langfuse observation type
_AS_TYPE: dict[str, str] = {
    "api": "span",
    "agent": "agent",
    "tool": "tool",
    "llm": "generation",
    "retrieval": "retriever",
    "safety": "guardrail",
    "final_response": "span",
    "persistence": "span",
}

_client: Any = None
_disabled_reason: str | None = None
_initialized = False
_turn_var: contextvars.ContextVar["Turn | None"] = contextvars.ContextVar(
    "langfuse_turn", default=None
)


def _cfg(key: str, default: str = "") -> str:
    """优先取 Settings（.env 由 pydantic 读取，不一定进 os.environ），再退环境变量。"""
    try:
        from src.core.config import get_settings

        val = getattr(get_settings(), key, None)
        if val is not None and str(val).strip():
            return str(val).strip()
    except Exception:
        pass
    return (os.getenv(key) or default).strip()


def client() -> Any:
    """返回 Langfuse 客户端；不可用时返回 None 并记下原因（只判定一次）。"""
    global _client, _disabled_reason, _initialized
    if _initialized:
        return _client
    _initialized = True

    if _cfg("LANGFUSE_ENABLED", "false").lower() not in ("1", "true", "yes", "on"):
        _disabled_reason = "LANGFUSE_ENABLED 未开启"
        return None

    public_key = _cfg("LANGFUSE_PUBLIC_KEY")
    secret_key = _cfg("LANGFUSE_SECRET_KEY")
    if not public_key or not secret_key:
        _disabled_reason = "缺少 LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY"
        logger.warning("Langfuse 未接入：%s", _disabled_reason)
        return None

    try:
        from langfuse import Langfuse

        _client = Langfuse(
            public_key=public_key,
            secret_key=secret_key,
            base_url=_cfg("LANGFUSE_BASE_URL", "http://localhost:3000"),
            environment=_cfg("LANGFUSE_ENVIRONMENT", "dev"),
        )
        _silence_context_noise()
    except Exception as e:  # pragma: no cover - 依赖缺失/异常配置
        _disabled_reason = f"初始化失败 {type(e).__name__}: {e}"
        logger.warning("Langfuse 初始化失败，已降级：%s", e)
        _client = None
    return _client


class _DropContextNoise(logging.Filter):
    """SDK 在"当前没有 active span"时会刷一条 warning。

    我们刻意在根 span 一建好就退出属性上下文（否则 close 阶段一定踩到它），
    这条日志属于预期内的噪声，过滤掉；其它 Langfuse 告警照常输出。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return "No active span in current context" not in record.getMessage()


def _silence_context_noise() -> None:
    lf_logger = logging.getLogger("langfuse")
    if not any(isinstance(f, _DropContextNoise) for f in lf_logger.filters):
        lf_logger.addFilter(_DropContextNoise())


def status() -> dict[str, Any]:
    return {"enabled": client() is not None, "reason": _disabled_reason}


def _should_sample() -> bool:
    """按 TRACE_SAMPLE_RATE 采样：>=1 全采，<=0 全不采，中间按比例。"""
    try:
        rate = float(_cfg("TRACE_SAMPLE_RATE", "1.0") or 1.0)
    except Exception:
        return True
    if rate >= 1.0:
        return True
    if rate <= 0.0:
        return False
    return random.random() < rate


@dataclass
class SpanHandle:
    """一个进行中的子节点（Langfuse observation）。"""

    name: str
    kind: str
    span_id: str
    started_at: float
    attributes: dict[str, Any] = field(default_factory=dict)
    _cm: Any = None
    _obs: Any = None
    _closed: bool = False

    def _update(self, **kwargs: Any) -> None:
        if self._obs is None:
            return
        try:
            self._obs.update(**kwargs)
        except Exception as e:
            logger.warning("langfuse observation update 失败：%s", e)


class Turn:
    """一轮对话的 trace 根节点 + 计数器。

    计数器（token / 工具调用 / 检索）原来自研版落 DB 聚合字段，
    现在统一在收尾时作为 trace 元数据和 usage_details 上报。
    """

    def __init__(
        self,
        *,
        trace_id: str = "",
        request_id: str = "",
        user_id: str = "",
        session_id: str = "",
        turn_id: str = "",
        entrypoint: str = "",
        agent_name: str = "supervisor",
        sampled: bool = True,
    ) -> None:
        self.trace_id = trace_id
        self.request_id = request_id
        self.user_id = user_id
        self.session_id = session_id
        self.turn_id = turn_id
        self.entrypoint = entrypoint
        self.agent_name = agent_name
        self.sampled = sampled
        self.status = "running"
        self.error_type: str | None = None
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.tool_calls = 0
        self.agent_calls = 0
        self.tool_names: list[str] = []
        self.retrieval_count = 0
        self.retrieval_sources: list[dict] = []
        self.grounded: bool | None = None
        self.safety_level = "info"
        self.safety_result = "allow"
        self.safety_rules: list[str] = []
        self.model_names: list[str] = []
        self.input_chars = 0
        self.output_chars = 0
        self.output_text = ""
        self.extra: dict[str, Any] = {}
        self.started_at = time.monotonic()
        self._attr_cm: Any = None
        self._obs_cm: Any = None
        self._obs: Any = None
        self._open: dict[str, SpanHandle] = {}
        self._finished = False

    # ── 子节点 ─────────────────────────────────────────────────────────
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
        input_text: str = "",
    ) -> SpanHandle | None:
        if not self.sampled:
            return None
        attrs = redact_payload(attributes or {})
        attrs.setdefault("agent_name", agent_name or self.agent_name)
        if tool_name:
            attrs["tool_name"] = tool_name
        if model_name:
            attrs["model_name"] = model_name
        if model_version:
            attrs["model_version"] = model_version
        attrs["span_id"] = uuid.uuid4().hex[:16]
        if parent_span_id:
            attrs["parent_span_id"] = parent_span_id

        handle = SpanHandle(
            name=name[:100],
            kind=kind,
            span_id=attrs["span_id"],
            started_at=time.monotonic(),
            attributes=attrs,
        )
        c = client()
        if c is not None:
            try:
                handle._cm = c.start_as_current_observation(
                    name=handle.name,
                    as_type=_AS_TYPE.get(kind, "span"),
                    metadata=attrs,
                    input=redact_text(input_text, 2000) if input_text else None,
                )
                handle._obs = handle._cm.__enter__()
            except Exception as e:
                logger.warning("langfuse 建 span 失败，该节点不上报：%s", e)
                handle._cm = None
                handle._obs = None
        self._open[handle.span_id] = handle
        return handle

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
        output_text: str = "",
    ) -> None:
        if span is None or span._closed:
            return
        span._closed = True
        self._open.pop(span.span_id, None)
        latency_ms = int((time.monotonic() - span.started_at) * 1000)
        attrs = {**span.attributes, **(attributes or {})}
        if agent_name:
            attrs["agent_name"] = agent_name
        attrs["latency_ms"] = latency_ms
        if status != "ok":
            attrs["status"] = status
        if error_type:
            attrs["error_type"] = error_type

        kwargs: dict[str, Any] = {"metadata": redact_payload(attrs)}
        if status in ("error", "timeout"):
            kwargs["level"] = "ERROR"
            kwargs["status_message"] = str(error_type or status)[:400]
        if output_text:
            kwargs["output"] = redact_text(output_text, 2000)
        if prompt_tokens or completion_tokens:
            kwargs["usage_details"] = {
                "input": int(prompt_tokens), "output": int(completion_tokens),
            }
        span._update(**kwargs)
        if span._cm is not None:
            try:
                span._cm.__exit__(None, None, None)
            except Exception as e:
                logger.warning("langfuse 关闭 span 失败：%s", e)

    # ── 计数器 ─────────────────────────────────────────────────────────
    def add_tokens(
        self, prompt_tokens: int, completion_tokens: int,
        model_name: str = "", model_version: str = "",
    ) -> None:
        self.prompt_tokens += int(prompt_tokens or 0)
        self.completion_tokens += int(completion_tokens or 0)
        if model_name and model_name not in self.model_names:
            self.model_names.append(model_name[:100])

    def count_tool(self, name: str) -> None:
        self.tool_calls += 1
        if name and name not in self.tool_names:
            self.tool_names.append(name[:64])

    def count_agent(self, name: str = "") -> None:
        self.agent_calls += 1

    def add_retrieval(self, engine: str, items: list[dict], top_k: int = 0) -> None:
        """记录一次检索。检索来源是 citation 的核对依据，只保留定位字段与摘句。"""
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
                "score_type": str(item.get("score_type") or "vector")[:16],
                "quote": redact_text(str(item.get("text") or ""), 120),
            })
        self.retrieval_sources.extend(safe_items)
        self.retrieval_count += len(safe_items)
        span = self.start_span(
            f"retrieval.{engine}", "retrieval",
            attributes={
                "engine": engine,
                "returned": len(safe_items),
                "top_k": top_k,
                "sources": safe_items[:10],
            },
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

    def add_span_snapshot(self, name: str, kind: str, latency_ms: int, **attributes: Any) -> None:
        """记录一个已完成的瞬时节点（如 Safety Engine 审核）。"""
        span = self.start_span(name, kind, attributes={**attributes, "latency_ms": latency_ms})
        self.end_span(span, status="ok")

    # ── 收尾 ───────────────────────────────────────────────────────────
    def finish(self, status: str = "success", error_type: str | None = None, output_text: str = "") -> None:
        if self._finished:
            return
        self._finished = True
        if status == "running":
            status = "success"
        self.status = status
        self.error_type = error_type
        if output_text:
            self.output_text = output_text
        if not self.sampled:
            return
        # 异常路径可能没正常退出上下文，残留节点按 error 收口
        for span in list(self._open.values()):
            self.end_span(span, status="error" if status == "error" else "ok")

        latency_ms = int((time.monotonic() - self.started_at) * 1000)
        if self._obs is not None:
            kwargs: dict[str, Any] = {
                "metadata": {
                    "status": status,
                    "error_type": error_type,
                    "latency_ms": latency_ms,
                    "tool_calls": self.tool_calls,
                    "tool_names": self.tool_names,
                    "agent_calls": self.agent_calls,
                    "retrieval_count": self.retrieval_count,
                    "safety_level": self.safety_level,
                    "safety_result": self.safety_result,
                    "safety_rules": self.safety_rules,
                    "grounded": self.grounded,
                    "input_chars": self.input_chars,
                    "output_chars": self.output_chars,
                    "request_id": self.request_id,
                    "turn_id": self.turn_id,
                    "entrypoint": self.entrypoint,
                    "model_name": (self.model_names[0] if self.model_names else ""),
                    **self.extra,
                },
                "usage_details": {
                    "input": self.prompt_tokens, "output": self.completion_tokens,
                },
            }
            if self.output_text:
                kwargs["output"] = redact_text(self.output_text, 2000)
            if status in ("error", "timeout"):
                kwargs["level"] = "ERROR"
                kwargs["status_message"] = str(error_type or status)[:400]
            try:
                self._obs.update(**kwargs)
            except Exception as e:
                logger.warning("langfuse 根节点收尾失败：%s", e)
        if self._obs_cm is not None:
            try:
                self._obs_cm.__exit__(None, None, None)
            except Exception as e:
                logger.warning("langfuse trace 关闭失败：%s", e)
            self._obs_cm = None
        if self._attr_cm is not None:  # 最后退出：与 start_trace 里的进入顺序相反
            try:
                self._attr_cm.__exit__(None, None, None)
            except Exception as e:
                logger.debug("langfuse 退出属性上下文失败：%s", e)
            self._attr_cm = None


# ── 对外 API（与旧自研实现同名，业务侧只换 import） ───────────────────────
def start_trace(
    *,
    request_id: str = "",
    user_id: str = "",
    session_id: str = "",
    turn_id: str = "",
    entrypoint: str = "",
    agent_name: str = "supervisor",
    input_text: str = "",
    sampled: bool | None = None,
    metadata: dict[str, Any] | None = None,
) -> tuple[Turn, contextvars.Token]:
    """开启一轮 trace，返回 (turn, token)。token 供 end_trace 复位上下文。

    metadata 里的键值（如 agent_version / prompt_version / model）会并进 trace 属性，
    用来回答"这一轮用的是哪个版本"——Langfuse 的 version 字段没人写，就靠 metadata。
    """
    sampled = _should_sample() if sampled is None else sampled
    turn = Turn(
        request_id=request_id,
        user_id=user_id,
        session_id=session_id,
        turn_id=turn_id,
        entrypoint=entrypoint,
        agent_name=agent_name,
        sampled=sampled,
    )
    token = _turn_var.set(turn)

    c = client()
    if c is not None and sampled:
        try:
            from langfuse import propagate_attributes

            # propagate_attributes 只用来把 user/session/trace_name 打到"当前 span"上，
            # 根 span 一建好就把属性上下文退出：这样 trace 上带上了归属信息，
            # 后续子节点靠父子关系继承，也不会在 close 阶段踩到"没有 active span"。
            attr_meta: dict[str, Any] = {"request_id": request_id, "turn_id": turn_id}
            attr_meta.update({k: v for k, v in (metadata or {}).items() if v not in (None, "")})
            attr_cm = propagate_attributes(
                user_id=user_id or None,
                session_id=session_id or None,
                metadata=attr_meta,
                trace_name=entrypoint or "chat.turn",
            )
            attr_cm.__enter__()
            turn._attr_cm = attr_cm
            turn._obs_cm = c.start_as_current_observation(
                name=entrypoint or "chat.turn",
                as_type="agent",
                input=redact_text(input_text, 2000) if input_text else None,
                metadata={"agent_name": agent_name, "turn_id": turn_id, **{k: v for k, v in (metadata or {}).items() if v not in (None, "")}},
            )
            turn._obs = turn._obs_cm.__enter__()
            try:
                turn.trace_id = c.get_current_trace_id() or turn.trace_id
            except Exception:
                pass
            # 注意：属性上下文必须一直活到本轮结束（在 Turn.finish 里最后退出）。
            # 提前退出会把 OTel 当前上下文回滚到"根 span 创建之前"，
            # 之后在别的 asyncio 任务里建的子 span 就找不到父节点，各自开一条新 trace。
        except Exception as e:
            logger.warning("langfuse 建 trace 失败，本轮不上报：%s", e)
            turn._obs = None
            turn._obs_cm = None
            turn._attr_cm = None
    return turn, token


def current_trace() -> Turn | None:
    return _turn_var.get()


def get_trace_id() -> str:
    turn = _turn_var.get()
    if turn is not None and turn.trace_id:
        return turn.trace_id
    c = client()
    if c is None:
        return ""
    try:
        return c.get_current_trace_id() or ""
    except Exception:
        return ""


def end_trace(
    turn: Turn | None = None,
    token: contextvars.Token | None = None,
    *,
    status: str = "success",
    error_type: str | None = None,
    output_text: str = "",
) -> None:
    turn = turn or _turn_var.get()
    if turn is not None:
        turn.finish(status=status, error_type=error_type, output_text=output_text)
    if token is not None:
        try:
            _turn_var.reset(token)
        except ValueError:
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
    input_text: str = "",
) -> AsyncIterator[SpanHandle | None]:
    """追踪一个异步阶段；异常自动标记 error 并记录异常类型。"""
    turn = current_trace()
    if turn is None or not turn.sampled:
        yield None
        return
    span = turn.start_span(
        name, kind, agent_name=agent_name, tool_name=tool_name,
        model_name=model_name, attributes=attributes, input_text=input_text,
    )
    try:
        yield span
    except BaseException as e:
        is_timeout = isinstance(e, TimeoutError) or type(e).__name__ == "CancelledError"
        turn.end_span(span, status="timeout" if is_timeout else "error", error_type=type(e).__name__)
        raise
    else:
        turn.end_span(span, status="ok")


def bind_span(span: SpanHandle | None, key: str, value: Any) -> None:
    """给进行中的 span 补充属性（值会脱敏）；收尾时整体上报。"""
    if span is None:
        return
    span.attributes[key] = redact_payload(value)


def log_context() -> dict[str, str]:
    """返回可直接 bind 到 loguru 的上下文字典。"""
    turn = current_trace()
    return {
        "request_id": turn.request_id if turn else "",
        "trace_id": get_trace_id(),
        "session_id": turn.session_id if turn else "",
        "turn_id": turn.turn_id if turn else "",
    }
