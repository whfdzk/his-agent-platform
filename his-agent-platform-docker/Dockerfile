# ============================================================
# his-agent-platform 应用镜像（生产）
# 多阶段构建：builder 安装依赖 → runtime 精简运行
# 构建上下文为项目根目录：
#   docker build -t his-agent:latest .
# ============================================================

FROM python:3.13-slim AS builder

WORKDIR /build
COPY requirements.txt ./
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt \
    && pip install --no-cache-dir --prefix=/install uvloop httptools

# ── 运行时镜像 ────────────────────────────────────────────────
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai

# 非 root 用户
RUN groupadd -r appuser && useradd -r -g appuser appuser \
    && mkdir -p /app/logs && chown -R appuser:appuser /app

COPY --from=builder /install /usr/local
WORKDIR /app

COPY src ./src
COPY alembic ./alembic
COPY alembic.ini scripts requirements.txt ./

USER appuser

EXPOSE 8000

# 双 worker + uvloop：状态全在 Redis/PG，进程无本地会话状态，可水平扩展
CMD ["python", "-m", "uvicorn", "src.main:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "2", "--loop", "uvloop", "--http", "httptools", \
     "--timeout-keep-alive", "75", "--no-access-log"]
