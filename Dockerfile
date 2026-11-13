# 后端 API 镜像（多阶段：依赖与运行时分层，便于复用缓存）
#
# 说明：前端（front/）是独立的 Vite 工程，生产由 Nginx 托管静态产物，
# 本镜像只负责 FastAPI 服务。

FROM python:3.13-slim AS deps
WORKDIR /app
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
# 先只拷依赖清单，代码变更不会击穿依赖层缓存
COPY requirements.txt .
RUN pip install --prefix=/install -r requirements.txt

FROM python:3.13-slim AS runtime
WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/usr/local/bin:$PATH"

COPY --from=deps /install /usr/local
COPY alembic.ini ./
COPY alembic ./alembic
COPY src ./src

# 非 root 运行：容器逃逸后的影响面更小
RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8080
# 生产不要 --reload；迁移通过独立的 job/命令执行（alembic upgrade head）
CMD ["python", "-m", "uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers"]
