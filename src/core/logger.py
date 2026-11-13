import sys
from pathlib import Path

from loguru import logger

from src.core.config import get_settings, Settings


def _inject_context(record) -> None:
    """把 request_id / trace_id 注入每条日志，便于按链路检索。

    延迟 import 以避免 core.logger ←→ core.tracing 的导入环。
    """
    try:
        from src.core.tracing import log_context
        # 1. 从 tracing 模块拿到当前请求上下文（request_id、trace_id 等）
        for key, value in log_context().items():
            record["extra"].setdefault(key, value)
    except Exception:
        pass
    for key in ("request_id", "trace_id", "session_id", "turn_id"):
        record["extra"].setdefault(key, "")


# 项目一启动，必须自己调用这个方法，配置好日志组件的工作规则
def setup_logger(settings: Settings) -> None:
    logger.remove()
    # logger.configure(patcher=_inject_context) — 给每条记录"打针"
    logger.configure(patcher=_inject_context)

    log_format = (
        "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | "
        "rid={extra[request_id]} tid={extra[trace_id]} | "
        "{name}:{function}:{line} - {message}"
    )

    # 控制台
    logger.add(
        sys.stdout,
        level=settings.LOG_LEVEL,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>rid={extra[request_id]}</cyan> <magenta>tid={extra[trace_id]}</magenta> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
            "<level>{message}</level>"
        ),
        colorize=True,
    )

    # 文件：访问日志与模型调用记录按 ACCESS_LOG_RETENTION_DAYS 保留
    log_dir = Path(settings.LOG_DIR)
    log_dir.mkdir(parents=True, exist_ok=True)
    logger.add(
        str(log_dir / "{time:YYYY-MM-DD}.log"),
        level=settings.LOG_LEVEL,
        format=log_format,
        rotation="00:00",
        retention=f"{settings.ACCESS_LOG_RETENTION_DAYS} days",
        compression="gz",
        encoding="utf-8",
    )
