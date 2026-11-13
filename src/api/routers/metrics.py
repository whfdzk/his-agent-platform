# src/api/routers/metrics.py
#
# Prometheus 指标端点（/metrics，由 Nginx 内网 ACL 保护）。
# prometheus_client 未安装时降级为手写文本格式，保证端点始终可用。

from __future__ import annotations

import secrets

from fastapi import APIRouter, Request, Response

from src.core.concurrency import llm_in_flight, sse_limiter
from src.core.config import get_settings
from src.core.exceptions import BizErrorCode, BizException

router = APIRouter(tags=["metrics"])

try:
    from prometheus_client import Gauge, Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST

    _HAVE_PROM = True
    SSE_CONNS = Gauge("sse_active_connections", "当前 SSE 活跃连接数（本进程）")
    LLM_IN_FLIGHT = Gauge("llm_requests_in_flight", "在途 LLM 请求数（本进程）")
    HTTP_REQUESTS = Counter("http_requests_total", "HTTP 请求总数", ["method", "path", "status"])
    HTTP_LATENCY = Histogram("http_request_duration_seconds", "HTTP 请求延迟", ["method", "path"])
    AGENT_TASKS = Counter("agent_tasks_total", "Agent 任务总数", ["agent", "status"])
    RAG_HITS = Counter("rag_hits_total", "RAG 检索命中数", ["category"])
except ImportError:  # pragma: no cover
    _HAVE_PROM = False


def _authorize(request: Request) -> None:
    """指标端点访问控制。

    Prometheus 抓取无法携带管理员 JWT，因此用共享令牌：
    - 配置了 METRICS_TOKEN：必须与 X-Metrics-Token 头一致；
    - 未配置且非 dev：直接拒绝（宁可抓不到指标，也不暴露内部运行态）。
    """
    settings = get_settings()
    expected = getattr(settings, "METRICS_TOKEN", "") or ""
    if expected:
        provided = request.headers.get("x-metrics-token", "")
        if not secrets.compare_digest(provided, expected):
            raise BizException(BizErrorCode.FORBIDDEN, "无权访问监控数据", status_code=403)
        return
    if settings.APP_ENV == "dev":
        return
    raise BizException(
        BizErrorCode.FORBIDDEN, "监控端点未配置 METRICS_TOKEN，已禁用", status_code=403,
    )


@router.get("/metrics")
async def metrics(request: Request) -> Response:
    _authorize(request)
    settings = get_settings()
    sse_active = sse_limiter.active
    in_flight = llm_in_flight()

    if _HAVE_PROM:
        SSE_CONNS.set(sse_active)
        LLM_IN_FLIGHT.set(in_flight)
        body = generate_latest()
        return Response(content=body, media_type=CONTENT_TYPE_LATEST)

    # 降级：手写 Prometheus 文本格式
    lines = [
        "# HELP sse_active_connections Current SSE connections (per process)",
        "# TYPE sse_active_connections gauge",
        f"sse_active_connections {sse_active}",
        "# HELP llm_requests_in_flight In-flight LLM requests (per process)",
        "# TYPE llm_requests_in_flight gauge",
        f"llm_requests_in_flight {in_flight}",
        "# HELP process_resident_memory_bytes Resident memory size",
        "# TYPE process_resident_memory_bytes gauge",
    ]
    try:
        import resource

        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
        lines.append(f"process_resident_memory_bytes {rss}")
    except Exception:
        pass
    lines.append(f"app_env{{env=\"{settings.APP_ENV}\"}} 1")
    return Response(content="\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")
