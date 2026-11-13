# src/api/routers/admin_stats.py
#
# 管理端：数据统计总览（P1）。
# - 用户量 / 会话量 / 消息量 / 调用量 / Token 消耗，一张接口喂 Dashboard
# - 明细统计复用既有 /stats/tokens、/stats/consultations，避免重复实现

from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_current_admin
from src.infra.database import get_db
from src.modules.chat.model import ChatMessage, Conversation
from src.modules.medical.model import Patient
from src.modules.ops.model import TokenUsage

router = APIRouter(
    prefix="/api/v1/admin/stats",
    tags=["admin-stats"],
    dependencies=[Depends(get_current_admin)],
)


@router.get("/overview")
async def overview(days: int = 7, db: AsyncSession = Depends(get_db)):
    """核心指标总览 + 近 N 天趋势（用户量/调用量/Token 按天分组）。"""
    days = min(max(days, 1), 90)
    since = datetime.now() - timedelta(days=days)

    total_users = (await db.execute(select(func.count()).select_from(Patient))).scalar() or 0
    enabled_users = (await db.execute(
        select(func.count()).select_from(Patient).where(Patient.enabled.is_(True))
    )).scalar() or 0
    total_conversations = (await db.execute(select(func.count()).select_from(Conversation))).scalar() or 0
    total_messages = (await db.execute(select(func.count()).select_from(ChatMessage))).scalar() or 0
    total_tokens = (await db.execute(
        select(func.coalesce(func.sum(TokenUsage.total_tokens), 0))
    )).scalar() or 0
    total_calls = (await db.execute(select(func.count()).select_from(TokenUsage))).scalar() or 0

    # 近 N 天每日 LLM 调用量与 Token 消耗
    trend_rows = (await db.execute(
        select(
            func.date(TokenUsage.created_at).label("day"),
            func.count().label("calls"),
            func.coalesce(func.sum(TokenUsage.total_tokens), 0).label("tokens"),
        )
        .where(TokenUsage.created_at >= since)
        .group_by(func.date(TokenUsage.created_at))
        .order_by(func.date(TokenUsage.created_at))
    )).all()

    # 近 N 天新增用户
    user_trend_rows = (await db.execute(
        select(func.date(Patient.created_at).label("day"), func.count())
        .where(Patient.created_at >= since)
        .group_by(func.date(Patient.created_at))
        .order_by(func.date(Patient.created_at))
    )).all()

    return {
        "overview": {
            "total_users": total_users,
            "enabled_users": enabled_users,
            "total_conversations": total_conversations,
            "total_messages": total_messages,
            "total_calls": total_calls,
            "total_tokens": int(total_tokens),
        },
        "usage_trend": [
            {"date": str(r.day), "calls": r.calls, "tokens": int(r.tokens)} for r in trend_rows
        ],
        "user_trend": [{"date": str(r.day), "count": r[1]} for r in user_trend_rows],
        "days": days,
    }
