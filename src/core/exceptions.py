from enum import IntEnum

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from loguru import logger

# starlette 约定：异常处理器必须接收 (request, exc) 两个参数


class BizErrorCode(IntEnum):
    """业务错误码。

    HTTP 状态码与业务码正交：状态码给网关/前端做通用判定（4xx/5xx），
    业务码给前端做具体分支。以前只有裸 HTTPException（body 是 {"detail": ...}），
    前端只能按状态行分支，且内部异常文本会被直接回传。
    """
    PARAM_ERROR = 40001           # 请求参数错误
    UNAUTHORIZED = 40002          # 未认证 / 凭证失效
    FORBIDDEN = 40003             # 无权限
    NOT_FOUND = 40004             # 资源不存在
    CONFLICT = 40009              # 资源冲突（重复提交 / 已注册 / 并发占用）
    RATE_LIMITED = 40029          # 请求过于频繁
    INTERNAL = 50000              # 服务器内部错误
    CONFIG_MISSING = 50001        # 关键配置缺失
    UPSTREAM_UNAVAILABLE = 50002  # 下游不可用（LLM 网关 / 对象存储 / 向量库）


class BizException(Exception):
    """业务异常：由 register_exception_handlers 统一转成 {code, message, data}。

    status_code 缺省 200 是历史契约（老客户端靠 body 里的 code 判错，改造前
    所有 BizException 都是 200）。新代码必须显式传 HTTP 状态码。
    """

    def __init__(
        self,
        code: int = BizErrorCode.PARAM_ERROR,
        message: str = "业务异常",
        status_code: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.code = int(code)
        self.message = message
        self.status_code = status_code
        # OAuth2 的 WWW-Authenticate 是协议要求，不能因为统一信封就丢掉
        self.headers = headers
        super().__init__(message)


def register_exception_handlers(app: FastAPI) -> None:

    @app.exception_handler(BizException)
    async def biz_exception_handler(request: Request, exc: BizException):
        return JSONResponse(
            status_code=exc.status_code,
            content={"code": exc.code, "message": exc.message, "data": None},
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError):
        # 与业务错误共用同一信封；具体字段错误只进日志，不回传内部结构
        logger.warning(f"请求参数校验失败: {exc.errors()}")
        return JSONResponse(
            status_code=422,
            content={
                "code": int(BizErrorCode.PARAM_ERROR),
                "message": "请求参数错误",
                "data": None,
            },
        )

    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception):
        logger.exception(f"Unhandled exception: {exc}")
        return JSONResponse(
            status_code=500,
            content={
                "code": int(BizErrorCode.INTERNAL),
                "message": "服务器内部错误",
                "data": None,
            },
        )
