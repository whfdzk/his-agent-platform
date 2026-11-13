# 纯 ASGI 日志中间件：不包装响应体（BaseHTTPMiddleware 会在 10k SSE 长连接下
# 引入额外 anyio 内存流与背压开销），仅记录请求起止与耗时，并注入 request_id。
import time
import uuid

from loguru import logger

from src.core.concurrency import request_id_var

# 不记日志的路径（探活/指标高频请求）
_SKIP_PATHS = {"/health", "/health/deps", "/metrics", "/favicon.ico"}


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
            request_id_var.reset(token)
