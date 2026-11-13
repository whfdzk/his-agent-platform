# src/api/routers/admin_users.py
#
# 管理端：用户（患者）管理。
# - 列表 / 搜索：姓名、手机号模糊匹配，支持状态过滤与分页
# - 详情：基本资料 + 会话数 + 预约数
# - 状态管理：启用 / 禁用（封禁后患者端登录被拒）
# - 基本资料维护：姓名、性别、年龄、过敏史等

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_current_admin
from src.core.exceptions import BizErrorCode, BizException
from src.infra.database import get_db
from src.modules.chat.model import Conversation
from src.modules.medical.model import Appointment, Patient
from src.modules.patient.model import PatientProfile
from src.modules.platform.model import UserMembership

router = APIRouter(
    prefix="/api/v1/admin/users",
    tags=["admin-users"],
    dependencies=[Depends(get_current_admin)],
)


def _mask_phone(phone: str | None) -> str:
    value = phone or ""
    return value[:3] + "****" + value[-4:] if len(value) >= 7 else value


def _serialize(p: Patient, conversations: int = 0, appointments: int = 0) -> dict:
    phone = p.phone or ""
    id_card = p.id_card or ""
    return {
        "id": p.id,
        "name": p.name,
        "phone": phone[:3] + "****" + phone[-4:] if len(phone) >= 7 else phone,
        "gender": p.gender,
        "age": p.age,
        "id_card": id_card[:4] + "********" + id_card[-2:] if len(id_card) >= 6 else id_card,
        "blood_type": p.blood_type,
        "allergy_history": p.allergy_history,
        "medical_history": p.medical_history,
        "enabled": p.enabled,
        "registered": bool(p.password_hash),
        "conversations": conversations,
        "appointments": appointments,
        "created_at": str(p.created_at) if p.created_at else None,
    }


class UserUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=100)
    gender: str | None = Field(default=None, max_length=10)
    age: int | None = Field(default=None, ge=0, le=150)
    blood_type: str | None = Field(default=None, max_length=10)
    allergy_history: str | None = None
    medical_history: str | None = None


@router.get("")
async def list_users(
    keyword: str | None = None,
    enabled: bool | None = None,
    page: int = 1,
    page_size: int = 20,
    db: AsyncSession = Depends(get_db),
):
    """用户列表：keyword 匹配姓名/手机号；enabled 过滤账号状态。"""
    page = max(page, 1)
    page_size = min(max(page_size, 1), 100)

    q = select(Patient)
    if keyword:
        like = f"%{keyword}%"
        q = q.where((Patient.name.like(like)) | (Patient.phone.like(like)))
    if enabled is not None:
        q = q.where(Patient.enabled == enabled)

    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar() or 0
    rows = (await db.execute(
        q.order_by(Patient.id.desc()).offset((page - 1) * page_size).limit(page_size)
    )).scalars().all()

    # 批量取会话/预约计数，避免 N+1
    ids = [p.id for p in rows]
    conv_counts: dict[int, int] = {}
    appt_counts: dict[int, int] = {}
    if ids:
        conv_rows = (await db.execute(
            select(Conversation.user_id, func.count()).where(Conversation.user_id.in_([str(i) for i in ids])).group_by(Conversation.user_id)
        )).all()
        conv_counts = {int(uid): c for uid, c in conv_rows if str(uid).isdigit()}
        appt_rows = (await db.execute(
            select(Appointment.patient_id, func.count()).where(Appointment.patient_id.in_(ids)).group_by(Appointment.patient_id)
        )).all()
        appt_counts = {pid: c for pid, c in appt_rows if pid is not None}

    return {
        "users": [
            _serialize(p, conv_counts.get(p.id, 0), appt_counts.get(p.id, 0)) for p in rows
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/{user_id}")
async def get_user(user_id: int, db: AsyncSession = Depends(get_db)):
    """患者详情聚合：基本资料 + 健康档案 + 最近会话/预约 + 会员状态。

    只读视图：管理端修改资料走 PUT，避免详情页误改数据。
    """
    p = await db.get(Patient, user_id)
    if not p:
        raise BizException(BizErrorCode.NOT_FOUND, "用户不存在", status_code=404)

    convs = (await db.execute(
        select(func.count()).select_from(Conversation).where(Conversation.user_id == str(user_id))
    )).scalar() or 0
    appts = (await db.execute(
        select(func.count()).select_from(Appointment).where(Appointment.patient_id == user_id)
    )).scalar() or 0

    profile = (await db.execute(
        select(PatientProfile).where(PatientProfile.patient_id == user_id).limit(1)
    )).scalar_one_or_none()

    recent_conversations = (await db.execute(
        select(Conversation)
        .where(Conversation.user_id == str(user_id))
        .order_by(Conversation.last_active_at.desc().nullslast(), Conversation.id.desc())
        .limit(5)
    )).scalars().all()

    recent_appointments = (await db.execute(
        select(Appointment)
        .where(Appointment.patient_id == user_id)
        .order_by(Appointment.id.desc())
        .limit(5)
    )).scalars().all()

    membership = (await db.execute(
        select(UserMembership).where(UserMembership.user_id == str(user_id)).limit(1)
    )).scalar_one_or_none()

    return {
        "user": _serialize(p, convs, appts),
        "profile": {
            "birth_date": str(profile.birth_date) if profile and profile.birth_date else None,
            "height_cm": profile.height_cm if profile else None,
            "weight_kg": profile.weight_kg if profile else None,
            "waist_cm": profile.waist_cm if profile else None,
            "marital_status": profile.marital_status if profile else None,
            "occupation": profile.occupation if profile else None,
            "city": profile.city if profile else None,
            "emergency_contact_name": profile.emergency_contact_name if profile else None,
            # 紧急联系人电话同属 PHI：管理端只展示掩码，避免截图/日志外泄
            "emergency_contact_phone": _mask_phone(profile.emergency_contact_phone) if profile else None,
            "surgery_history": profile.surgery_history if profile else None,
            "family_history": profile.family_history if profile else None,
            "chronic_diseases": profile.chronic_diseases if profile else None,
            "smoking": profile.smoking if profile else None,
            "drinking": profile.drinking if profile else None,
        },
        "recent_conversations": [
            {
                "id": c.id,
                "session_id": c.session_id,
                "title": c.title,
                "status": c.status,
                "last_active_at": str(c.last_active_at) if c.last_active_at else None,
            }
            for c in recent_conversations
        ],
        "recent_appointments": [
            {
                "id": a.id,
                "appointment_no": a.appointment_no,
                "department": a.department,
                "doctor_name": a.doctor_name,
                "date": a.date,
                "time_slot": a.time_slot,
                "status": a.status,
            }
            for a in recent_appointments
        ],
        "membership": (
            {
                "plan_name": membership.plan_name,
                "start_at": membership.start_at,
                "expire_at": membership.expire_at,
                "status": membership.status,
            }
            if membership
            else None
        ),
    }


@router.put("/{user_id}")
async def update_user(user_id: int, req: UserUpdate, db: AsyncSession = Depends(get_db)):
    """基本资料维护。"""
    p = await db.get(Patient, user_id)
    if not p:
        raise BizException(BizErrorCode.NOT_FOUND, "用户不存在", status_code=404)
    for k, v in req.model_dump(exclude_none=True).items():
        setattr(p, k, v)
    await db.flush()
    return {"message": "更新成功", "user": _serialize(p)}


@router.patch("/{user_id}/status")
async def update_user_status(user_id: int, enabled: bool, db: AsyncSession = Depends(get_db)):
    """账号状态管理：禁用后患者端登录被拒。"""
    p = await db.get(Patient, user_id)
    if not p:
        raise BizException(BizErrorCode.NOT_FOUND, "用户不存在", status_code=404)
    p.enabled = enabled
    await db.flush()
    return {"message": "已启用" if enabled else "已禁用", "user": _serialize(p)}
