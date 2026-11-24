# src/api/routers/observability.py
#
# 运维侧观测接口（仅管理端）。
#
# 链路轨迹已统一到 Langfuse（自托管，见 src/core/langfuse_tracing.py），
# 本文件不再提供 /traces 查询 —— 查链路请用 Langfuse UI，按 session_id / user_id 过滤。
# 这里只保留两类"业务侧自己算、Langfuse 给不了"的指标：
#   - 安全事件保留期（safety_events 清理与概况）
#   - 在线产品指标：转人工率、重复提问率

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_current_admin
from src.infra.database import get_db
from src.modules.ops.retention import purge_expired, retention_stats

router = APIRouter(
    prefix="/api/v1/observability",
    tags=["observability"],
    dependencies=[Depends(get_current_admin)],
)


@router.get("/retention")
async def get_retention(db: AsyncSession = Depends(get_db)):
    return await retention_stats(db)


@router.get("/online-metrics")
async def get_online_metrics(days: int = 7, db: AsyncSession = Depends(get_db)):
    """在线产品指标：转人工率、重复提问率（按会话聚合，回算历史，无需额外埋点）。"""
    from src.modules.ops.online_metrics import metrics as online_metrics
    return await online_metrics(db, days=max(1, min(days, 90)))


@router.post("/retention/purge")
async def run_retention_purge(db: AsyncSession = Depends(get_db)):
    """按保留周期清理过期安全事件（可挂定时任务每日执行）。"""
    return await purge_expired(db)
