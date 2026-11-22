# src/api/routers/guide.py
#
# 就医辅助的只读查询接口：科室介绍、医院信息。
#
# 这两类都是公开机构信息，不需要登录即可查询（不像患者档案要做行级隔离）；
# 写操作（挂号预约）走 /api/v1/appointments。

from __future__ import annotations

import time
from fastapi import APIRouter, Depends
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.exceptions import BizErrorCode, BizException
from src.infra.database import get_db
from src.modules.medical.model import Department, Disease, Hospital

router = APIRouter(prefix="/api/v1/guide", tags=["guide"])

# 缓存科室疾病数量（5分钟过期）
_disease_count_cache: dict[int, int] = {}
_disease_count_cache_time: float = 0
_CACHE_TTL = 300  # 5分钟


async def get_disease_counts(db: AsyncSession) -> dict[int, int]:
    """获取科室疾病数量（带缓存）。"""
    global _disease_count_cache, _disease_count_cache_time
    now = time.time()

    if now - _disease_count_cache_time < _CACHE_TTL and _disease_count_cache:
        return _disease_count_cache

    _disease_count_cache = dict(
        (
            await db.execute(
                select(Disease.department_id, func.count(Disease.id)).group_by(Disease.department_id)
            )
        ).all()
    )
    _disease_count_cache_time = now
    return _disease_count_cache


def _serialize_hospital(h: Hospital) -> dict:
    return {
        "id": h.id,
        "name": h.name,
        "level": h.level,
        "hospital_type": h.hospital_type,
        "province": h.province,
        "city": h.city,
        "address": h.address,
        "phone": h.phone,
        "emergency_phone": h.emergency_phone,
        "open_hours": h.open_hours,
        "intro": h.intro,
        "departments": h.departments,
    }


@router.get("/departments")
async def list_departments(
    keyword: str = "",
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
):
    """科室列表/搜索：带该科室疾病数量，便于前端做科室导航。"""
    q = select(Department)
    if keyword.strip():
        kw = f"%{keyword.strip()}%"
        q = q.where(or_(Department.name.like(kw), Department.description.like(kw)))
    rows = (await db.execute(q.order_by(Department.id.asc()).limit(max(1, min(limit, 200))))).scalars().all()

    counts = await get_disease_counts(db)
    return {
        "departments": [
            {
                "id": d.id,
                "name": d.name,
                "description": d.description,
                "disease_count": int(counts.get(d.id, 0)),
            }
            for d in rows
        ],
        "total": len(rows),
    }


@router.get("/departments/{department_id}")
async def get_department(department_id: int, db: AsyncSession = Depends(get_db)):
    """科室详情：简介 + 该科室常见疾病。"""
    dept = await db.get(Department, department_id)
    if dept is None:
        raise BizException(BizErrorCode.NOT_FOUND, "科室不存在", status_code=404)

    diseases = (
        await db.execute(
            select(Disease.name).where(Disease.department_id == dept.id).limit(20)
        )
    ).scalars().all()
    return {
        "department": {
            "id": dept.id,
            "name": dept.name,
            "description": dept.description,
            "diseases": list(diseases),
        }
    }


@router.get("/hospitals")
async def list_hospitals(
    keyword: str = "",
    city: str = "",
    limit: int = 20,
    db: AsyncSession = Depends(get_db),
):
    """医院信息查询：按名称关键词或城市过滤。"""
    q = select(Hospital)
    if keyword.strip():
        kw = f"%{keyword.strip()}%"
        q = q.where(or_(Hospital.name.like(kw), Hospital.intro.like(kw)))
    if city.strip():
        q = q.where(Hospital.city.like(f"%{city.strip()}%"))
    rows = (await db.execute(q.order_by(Hospital.id.asc()).limit(max(1, min(limit, 100))))).scalars().all()
    return {"hospitals": [_serialize_hospital(h) for h in rows], "total": len(rows)}
