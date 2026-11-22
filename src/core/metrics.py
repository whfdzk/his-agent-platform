# src/core/metrics.py
#
# Prometheus 指标统一定义处。
#
# 放在 core 层的原因：指标要被 API 中间件、LLM 回调、知识检索三处共同写入，
# 而这三处都不能反向依赖 api/routers。routers/metrics.py 只负责把 registry
# 导出成 HTTP 响应。
#
# prometheus_client 未安装时全部降级为 no-op，调用方无需做任何判断。

from __future__ import annotations

try:
    from prometheus_client import (
        CONTENT_TYPE_LATEST,
        Counter,
        Gauge,
        Histogram,
        generate_latest,
    )

    _HAVE_PROM = True

    SSE_CONNS = Gauge("sse_active_connections", "当前 SSE 活跃连接数（本进程）")
    LLM_IN_FLIGHT = Gauge("llm_requests_in_flight", "在途 LLM 请求数（本进程）")

    HTTP_REQUESTS = Counter(
        "http_requests_total", "HTTP 请求总数", ["method", "path", "status"],
    )
    # path 已按路由前缀归一化（见 middlewares.logging._path_label），
    # 避免每个 session_id/用户 ID 各成一条时间序列导致基数爆炸
    HTTP_LATENCY = Histogram(
        "http_request_duration_seconds", "HTTP 请求延迟",
        ["method", "path"],
        buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60),
    )

    # 首 token 延迟与整段完成延迟共用一张直方图：观察秒级分布，区分 P50/P95/P99
    LLM_LATENCY = Histogram(
        "llm_call_duration_seconds", "单次 LLM 调用耗时",
        ["model", "stream"],
        buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 3, 5, 8, 12, 20, 40),
    )
    RAG_LATENCY = Histogram(
        "rag_retrieval_duration_seconds", "知识检索耗时",
        ["engine"],
        buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 3, 5, 10),
    )

    AGENT_TASKS = Counter("agent_tasks_total", "Agent 任务总数", ["agent", "status"])
    RAG_HITS = Counter("rag_hits_total", "RAG 检索命中数", ["category"])
    TOOL_CALLS = Counter("tool_calls_total", "工具调用总数", ["tool", "status"])
except ImportError:  # pragma: no cover
    _HAVE_PROM = False
    CONTENT_TYPE_LATEST = "text/plain; version=0.0.4"
    generate_latest = None


def observe_http(method: str, path: str, status: int, elapsed_s: float) -> None:
    """记录一次 HTTP 请求：总数 + 延迟分布。"""
    if not _HAVE_PROM:
        return
    try:
        HTTP_REQUESTS.labels(method or "GET", path or "/", str(status or 0)).inc()
        HTTP_LATENCY.labels(method or "GET", path or "/").observe(max(elapsed_s, 0.0))
    except Exception:
        pass


def observe_llm(model: str, elapsed_s: float, stream: bool = False) -> None:
    """记录一次 LLM 调用耗时（含失败调用：失败也要能看出上游变慢）。"""
    if not _HAVE_PROM:
        return
    try:
        LLM_LATENCY.labels(model or "unknown", "stream" if stream else "sync").observe(
            max(elapsed_s, 0.0)
        )
    except Exception:
        pass


def observe_rag(engine: str, elapsed_s: float, hit_count: int = 0) -> None:
    """记录一次知识检索：耗时 + 命中数。"""
    if not _HAVE_PROM:
        return
    try:
        RAG_LATENCY.labels(engine or "unknown").observe(max(elapsed_s, 0.0))
        RAG_HITS.labels(engine or "unknown").inc(max(hit_count, 0))
    except Exception:
        pass


def observe_tool(tool: str, ok: bool = True) -> None:
    if not _HAVE_PROM:
        return
    try:
        TOOL_CALLS.labels(tool or "unknown", "ok" if ok else "error").inc()
    except Exception:
        pass
