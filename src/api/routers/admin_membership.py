# src/api/routers/admin_membership.py
#
# 管理端：会员管理（P2）。
# - 套餐 CRUD 与上下架
# - 订单列表（人工核销 / 后续对接支付）
# - 用户订阅列表

from __future__ import annotations

import json
import random
import string
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_current_admin
from src.core.exceptions import BizErrorCode, BizException
from src.infra.database import get_db
from src.modules.platform.model import MembershipOrder, MembershipPlan, UserMembership

router = APIRouter(
    prefix="/api/v1/admin/memberships",
    tags=["admin-memberships"],
    dependencies=[Depends(get_current_admin)],
)


def _serialize_plan(p: MembershipPlan) -> dict:
    try:
        benefits = json.loads(p.benefits) if p.benefits else []
    except (ValueError, TypeError):
        benefits = []
    return {
        "id": p.id,
        "name": p.name,
        "price_cents": p.price_cents,
        "duration_days": p.duration_days,
        "benefits": benefits,
        "enabled": p.enabled,
        "sort_order": p.sort_order,
        "created_at": str(p.created_at) if p.created_at else None,
    }


class PlanUpsert(BaseModel):
    name: str = Field(..., max_length=100)
    price_cents: int = Field(default=0, ge=0, description="价格（分）")
    duration_days: int = Field(default=30, ge=1)
    benefits: list[str] = Field(default_factory=list, description="权益列表")
    enabled: bool = True
    sort_order: int = 0


class OrderCreate(BaseModel):
    """人工核销创建订单（对接支付前）。"""
    user_id: str
    plan_id: int


@router.get("/plans")
async def list_plans(db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(
        select(MembershipPlan).order_by(MembershipPlan.sort_order, MembershipPlan.id)
    )).scalars().all()
    return {"plans": [_serialize_plan(p) for p in rows], "total": len(rows)}


@router.post("/plans")
async def create_plan(req: PlanUpsert, db: AsyncSession = Depends(get_db)):
    plan = MembershipPlan(
        name=req.name, price_cents=req.price_cents, duration_days=req.duration_days,
        benefits=json.dumps(req.benefits, ensure_ascii=False),
        enabled=req.enabled, sort_order=req.sort_order,
    )
    db.add(plan)
    await db.flush()
    return {"message": "创建成功", "plan": _serialize_plan(plan)}


@router.put("/plans/{plan_id}")
async def update_plan(plan_id: int, req: PlanUpsert, db: AsyncSession = Depends(get_db)):
    plan = await db.get(MembershipPlan, plan_id)
    if not plan:
        raise BizException(BizErrorCode.NOT_FOUND, "套餐不存在", status_code=404)
    plan.name = req.name
    plan.price_cents = req.price_cents
    plan.duration_days = req.duration_days
    plan.benefits = json.dumps(req.benefits, ensure_ascii=False)
    plan.enabled = req.enabled
    plan.sort_order = req.sort_order
    await db.flush()
    return {"message": "更新成功", "plan": _serialize_plan(plan)}


@router.delete("/plans/{plan_id}")
async def delete_plan(plan_id: int, db: AsyncSession = Depends(get_db)):
    plan = await db.get(MembershipPlan, plan_id)
    if not plan:
        raise BizException(BizErrorCode.NOT_FOUND, "套餐不存在", status_code=404)
    # 有订单引用时只做下架，保证订单快照可追溯
    order_count = (await db.execute(
        select(func.count()).select_from(MembershipOrder).where(MembershipOrder.plan_id == plan_id)
    )).scalar() or 0
    if order_count:
        plan.enabled = False
        await db.flush()
        return {"message": f"已有 {order_count} 笔订单引用，已改为下架"}
    await db.delete(plan)
    await db.flush()
    return {"message": "删除成功"}


@router.get("/orders")
async def list_orders(
    status: str | None = None,
    user_id: str | None = None,
    page: int = 1,
    page_size: int = 20,
    db: AsyncSession = Depends(get_db),
):
    page = max(page, 1)
    page_size = min(max(page_size, 1), 100)
    q = select(MembershipOrder)
    if status:
        q = q.where(MembershipOrder.status == status)
    if user_id:
        q = q.where(MembershipOrder.user_id == user_id)
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar() or 0
    rows = (await db.execute(
        q.order_by(MembershipOrder.id.desc()).offset((page - 1) * page_size).limit(page_size)
    )).scalars().all()
    return {
        "orders": [
            {
                "id": o.id,
                "order_no": o.order_no,
                "user_id": o.user_id,
                "plan_id": o.plan_id,
                "plan_name": o.plan_name,
                "amount_cents": o.amount_cents,
                "status": o.status,
                "created_at": str(o.created_at) if o.created_at else None,
            }
            for o in rows
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.post("/orders")
async def create_order(req: OrderCreate, db: AsyncSession = Depends(get_db)):
    """人工核销：创建已支付订单并开通/顺延会员。"""
    plan = await db.get(MembershipPlan, req.plan_id)
    if not plan:
        raise BizException(BizErrorCode.NOT_FOUND, "套餐不存在", status_code=404)

    order_no = "MB" + datetime.now().strftime("%Y%m%d%H%M%S") + "".join(
        random.choices(string.ascii_uppercase + string.digits, k=4)
    )
    order = MembershipOrder(
        order_no=order_no, user_id=req.user_id, plan_id=plan.id,
        plan_name=plan.name, amount_cents=plan.price_cents, status="paid",
    )
    db.add(order)

    # 开通/续费：同用户单条订阅记录，未过期顺延，过期/不存在则重新起算
    now = datetime.now(timezone.utc)
    sub = (await db.execute(
        select(UserMembership).where(UserMembership.user_id == req.user_id).limit(1)
    )).scalar_one_or_none()
    base = now
    if sub and sub.expire_at:
        try:
            prev_expire = datetime.fromisoformat(sub.expire_at)
            if prev_expire > now:
                base = prev_expire
        except ValueError:
            pass
    expire_at = base + timedelta(days=plan.duration_days)
    start_at = now
    if sub:
        sub.plan_id = plan.id
        sub.plan_name = plan.name
        sub.start_at = sub.start_at or start_at.isoformat(timespec="seconds")
        sub.expire_at = expire_at.isoformat(timespec="seconds")
        sub.status = "active"
    else:
        db.add(UserMembership(
            user_id=req.user_id, plan_id=plan.id, plan_name=plan.name,
            start_at=start_at.isoformat(timespec="seconds"),
            expire_at=expire_at.isoformat(timespec="seconds"), status="active",
        ))
    await db.flush()
    return {"message": "订单已核销，会员已开通", "order_no": order_no}


@router.get("/subscriptions")
async def list_subscriptions(db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(
        select(UserMembership).order_by(UserMembership.expire_at.desc().nullslast()).limit(200)
    )).scalars().all()
    return {
        "subscriptions": [
            {
                "id": s.id,
                "user_id": s.user_id,
                "plan_name": s.plan_name,
                "start_at": s.start_at,
                "expire_at": s.expire_at,
                "status": s.status,
            }
            for s in rows
        ],
        "total": len(rows),
    }
