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

from src.api.deps import AdminIdentity, get_current_admin
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
async def overview(
    days: int = 7,
    admin: AdminIdentity = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """核心指标总览 + 近 N 天趋势（用户量/调用量/Token 按天分组）。

    用户/会话/消息/调用量/Token 全部按管理员所属租户过滤（token_usage 已加
    tenant_id，见迁移 f3a9c1d7b5e2）。
    """
    days = min(max(days, 1), 90)
    since = datetime.now() - timedelta(days=days)
    tenant = admin.tenant_id

    total_users = (
        await db.execute(select(func.count()).select_from(Patient).where(Patient.tenant_id == tenant))
    ).scalar() or 0
    enabled_users = (await db.execute(
        select(func.count()).select_from(Patient).where(
            Patient.enabled.is_(True), Patient.tenant_id == tenant,
        )
    )).scalar() or 0
    total_conversations = (
        await db.execute(
            select(func.count()).select_from(Conversation)
            .where(Conversation.tenant_id == tenant)
        )
    ).scalar() or 0
    total_messages = (
        await db.execute(
            select(func.count()).select_from(ChatMessage)
            .where(ChatMessage.tenant_id == tenant)
        )
    ).scalar() or 0
    total_tokens = (await db.execute(
        select(func.coalesce(func.sum(TokenUsage.total_tokens), 0))
        .where(TokenUsage.tenant_id == tenant)
    )).scalar() or 0
    total_calls = (await db.execute(
        select(func.count()).select_from(TokenUsage).where(TokenUsage.tenant_id == tenant)
    )).scalar() or 0

    # 近 N 天每日 LLM 调用量与 Token 消耗
    trend_rows = (await db.execute(
        select(
            func.date(TokenUsage.created_at).label("day"),
            func.count().label("calls"),
            func.coalesce(func.sum(TokenUsage.total_tokens), 0).label("tokens"),
        )
        .where(TokenUsage.created_at >= since, TokenUsage.tenant_id == tenant)
        .group_by(func.date(TokenUsage.created_at))
        .order_by(func.date(TokenUsage.created_at))
    )).all()

    # 近 N 天新增用户
    user_trend_rows = (await db.execute(
        select(func.date(Patient.created_at).label("day"), func.count())
        .where(Patient.created_at >= since, Patient.tenant_id == tenant)
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
