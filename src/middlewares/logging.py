# 纯 ASGI 日志中间件：不包装响应体（BaseHTTPMiddleware 会在 10k SSE 长连接下
# 引入额外 anyio 内存流与背压开销），仅记录请求起止与耗时，并注入 request_id。
import re
import time
import uuid

from loguru import logger

from src.core.concurrency import request_id_var
from src.core.metrics import observe_http

# 不记日志的路径（探活/指标高频请求）
_SKIP_PATHS = {"/health", "/health/deps", "/metrics", "/favicon.ico"}

# 路径段里出现数字 / 长十六进制 / 超长串，一律视作业务 ID，归一化为 {id}，
# 否则 /admin/users/1、/history/abc... 会让指标时间序列基数爆炸
_HEX_ID_RE = re.compile(r"^[0-9a-fA-F]{16,}$")
_MAX_LABEL_SEGMENTS = 6


def _path_label(path: str) -> str:
    """把真实路径压成低基数标签，用于 Prometheus 的 path 维度。"""
    segments = []
    for seg in (path or "/").split("/"):
        if not seg:
            continue
        if seg.isdigit() or _HEX_ID_RE.match(seg) or len(seg) > 24:
            segments.append("{id}")
        else:
            segments.append(seg)
        if len(segments) >= _MAX_LABEL_SEGMENTS:
            break
    return "/" + "/".join(segments) if segments else "/"


class LoggingMiddleware:
    """ASGI 中间件：request_id 生成 + 访问日志（无响应体包装）。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path in _SKIP_PATHS:
            await self.app(scope, receive, send)
            return

        request_id = uuid.uuid4().hex[:16]
        token = request_id_var.set(request_id)
        start = time.perf_counter()
        method = scope.get("method", "")
        status_holder = {"status": 0}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                # request_id 回传客户端，便于排障对账
                headers = list(message.get("headers") or [])
                headers.append((b"x-request-id", request_id.encode()))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            elapsed = (time.perf_counter() - start) * 1000
            logger.info(
                f"[{request_id}] {method} {path} "
                f"status={status_holder['status']} {elapsed:.2f}ms"
            )
            # 指标写入与日志同源：同一份耗时，避免两处各计一次产生偏差
            observe_http(
                method,
                _path_label(path),
                status_holder["status"],
                elapsed / 1000.0,
            )
            request_id_var.reset(token)
