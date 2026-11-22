# src/api/routers/auth.py
#
# 患者端登录/注册 API：
# - POST /register：手机号+密码注册，写入 patients 表（若手机号已有档案且未设密码则绑定该档案）
# - POST /login   ：手机号+密码登录，签发 JWT
# - GET    /me    ：凭 Bearer token 返回当前患者信息
#
# 说明：patients.phone 为登录账号；应用层校验唯一（历史数据可能存在重复 phone，
# 不加 DB 级 unique 约束，以迁移安全为先）。

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, Header, Request, Response, status
from loguru import logger

from src.api.deps import ADMIN_ROLES, TenantIdentity, assert_token_active, get_current_tenant
from src.api.rate_limit import enforce_login_rate
from src.core.exceptions import BizErrorCode, BizException
from src.core.permissions import KIND_PATIENT
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.infra.database import get_db
from src.modules.medical.model import Patient
from src.modules.rbac import service as rbac
from src.utils import token_revocation as revocation
from src.utils.password_utils import hash_password, verify_password
from src.utils.jwt_utils import encode_jwt, token_remaining_seconds, verify_jwt
from src.utils.masking import mask_phone

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


def _serialize(p: Patient) -> dict:
    return {
        "id": p.id,
        "name": p.name,
        "phone": mask_phone(p.phone),
        "gender": p.gender,
        "age": p.age,
    }


class PatientProfile(BaseModel):
    """对外返回的患者信息；手机号一律为脱敏后的值。"""
    id: int
    name: str
    phone: str = Field(description="脱敏手机号，如 138****5678")
    gender: str | None = None
    age: int | None = None


class AuthResponse(BaseModel):
    message: str
    token: str
    user: PatientProfile


class MeResponse(BaseModel):
    user: PatientProfile


def _issue_token(
    p: Patient, *, roles: tuple[str, ...], role: str, scopes: frozenset[str], tenant_id: int,
) -> str:
    # payload 不放敏感数据，仅放展示与查询所需的标识
    # sub 必须为用户唯一标识字符串：chat/session 等端点从 sub 解析 user_id
    # roles/scopes 是 RBAC 判定依据：请求期只读 token，不再查库（见 src/core/permissions.py）
    # tid 是 C1 租户：后续接口按 token 里的租户过滤，不再信任请求体自报
    return encode_jwt({
        "sub": str(p.id),
        "patient_id": p.id,
        "name": p.name,
        "phone": p.phone or "",
        "kind": KIND_PATIENT,
        "role": role,
        "roles": list(roles),
        "scopes": sorted(scopes),
        "tid": int(tenant_id),
    })


class RegisterRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    phone: str = Field(pattern=r"^1[3-9]\d{9}$", description="手机号作为登录账号")
    password: str = Field(min_length=6, max_length=64)
    gender: str | None = None
    age: int | None = Field(default=None, ge=0, le=150)


class LoginRequest(BaseModel):
    phone: str
    password: str


def _client_ip(request: Request) -> str:
    """客户端 IP：依赖 uvicorn --proxy-headers + forwarded-allow-ips 还原真实地址。

    这里不手工解析 X-Forwarded-For —— 未经可信代理校验的头部可被客户端任意伪造，
    反而会让限流被轻易绕过。
    """
    return request.client.host if request.client else ""


@router.post("/register", response_model=AuthResponse)
async def register(
    req: RegisterRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    tenant: TenantIdentity = Depends(get_current_tenant),
):
    """注册：在请求所属租户下创建患者档案并直接登录。

    C1：手机号唯一性收窄到租户内 —— 同一手机号可在不同医院各有一个账号，
    所以查询条件必须是 (tenant_id, phone)，不能只按 phone。
    """
    # 注册同样纳入限流：否则会成为批量建号/探测手机号是否已注册的入口
    # 限流键带租户，避免 A 医院把 B 医院同号患者的尝试额度耗掉
    await enforce_login_rate(f"{tenant.id}:{req.phone}", _client_ip(request))
    existing = (
        await db.execute(
            select(Patient)
            .where(Patient.tenant_id == tenant.id, Patient.phone == req.phone)
            .limit(1)
        )
    ).scalar_one_or_none()

    if existing is not None and existing.password_hash:
        raise BizException(BizErrorCode.CONFLICT, "该手机号已注册，请直接登录", status_code=409)

    if existing is not None:
        # 本租户下手机号已有历史档案但未设置密码：绑定并补充信息
        existing.name = req.name
        existing.gender = req.gender or existing.gender
        existing.age = req.age if req.age is not None else existing.age
        existing.password_hash = hash_password(req.password)
        patient = existing
    else:
        patient = Patient(
            name=req.name,
            phone=req.phone,
            password_hash=hash_password(req.password),
            gender=req.gender,
            age=req.age,
            # 显式写 tenant_id：依赖列默认值会让新租户的数据静默落进默认租户
            tenant_id=tenant.id,
        )
        db.add(patient)

    await db.flush()
    # 新账号补默认角色（patient），否则 token 里只有兜底 scopes，管理端看不到授权记录
    await rbac.ensure_default_grant(db, KIND_PATIENT, patient.id)
    roles, role, scopes = await rbac.identity_of(db, KIND_PATIENT, patient.id)
    return {
        "message": "注册成功",
        "token": _issue_token(
            patient, roles=roles, role=role, scopes=scopes, tenant_id=tenant.id,
        ),
        "user": _serialize(patient),
    }


@router.post("/login", response_model=AuthResponse)
async def login(
    req: LoginRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    tenant: TenantIdentity = Depends(get_current_tenant),
):
    # 账号 + IP 双维度限流：撞库/密码喷洒在到达密码校验前就被挡住
    await enforce_login_rate(f"{tenant.id}:{req.phone}", _client_ip(request))
    patient = (
        await db.execute(
            select(Patient)
            .where(Patient.tenant_id == tenant.id, Patient.phone == req.phone)
            .limit(1)
        )
    ).scalar_one_or_none()

    # 账号不存在与密码错误返回同一文案：不区分可枚举账号
    if patient is None or not patient.password_hash:
        raise BizException(BizErrorCode.UNAUTHORIZED, "账号或密码错误", status_code=401)
    if not verify_password(req.password, patient.password_hash):
        raise BizException(BizErrorCode.UNAUTHORIZED, "账号或密码错误", status_code=401)
    if not getattr(patient, "enabled", True):
        raise BizException(BizErrorCode.FORBIDDEN, "账号已被禁用，请联系管理员", status_code=403)

    roles, role, scopes = await rbac.identity_of(db, KIND_PATIENT, patient.id)
    return {
        "message": "登录成功",
        "token": _issue_token(
            patient, roles=roles, role=role, scopes=scopes, tenant_id=tenant.id,
        ),
        "user": _serialize(patient),
    }


class LogoutRequest(BaseModel):
    """登出参数：all_devices=true 时同时让该账号其它设备上的 token 失效。"""

    all_devices: bool = False


def _decode_bearer_quietly(authorization: str | None) -> dict | None:
    """尽力解析 Bearer token；任何异常都返回 None（登出必须幂等）。"""
    if not authorization or not authorization.startswith("Bearer "):
        return None
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        return None
    try:
        return verify_jwt(token)
    except Exception as e:
        logger.info(f"登出时 token 已失效，按已登出处理: {type(e).__name__}")
        return None


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def logout(
    req: LogoutRequest | None = None,
    authorization: str | None = Header(default=None, description="Bearer <token>"),
):
    """患者端登出。

    幂等：token 缺失 / 非法 / 已过期一律返回 204，避免前端因登出失败卡在已登录态。
    正常路径下会把当前 token 的 jti 写入 Redis 撤销名单，
    使这份凭证在剩余有效期内立即失效（不再依赖前端自觉丢弃）。
    """
    payload = _decode_bearer_quietly(authorization)
    if payload is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    patient_id = str(payload.get("sub") or "")
    jti = payload.get("jti")
    if jti:
        await revocation.revoke_jti(str(jti), token_remaining_seconds(payload))

    all_devices = bool(req and req.all_devices)
    if all_devices and patient_id:
        # 时间精度为秒：刚签发的 token 也可能落在时间窗内，重新登录即可获得新 token
        await revocation.revoke_session_before("patient", patient_id, time.time())

    logger.info(f"患者登出 patient_id={patient_id or '-'} all_devices={all_devices}")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me", response_model=MeResponse)
async def me(
    authorization: str | None = Header(default=None, description="Bearer <token>"),
    db: AsyncSession = Depends(get_db),
):
    # 必须是 Header：裸 str 形参会被 FastAPI 当成 query 参数，
    # Authorization 头永远读不到 → 登录用户每次刷新都是 401
    if not authorization or not authorization.startswith("Bearer "):
        raise BizException(BizErrorCode.UNAUTHORIZED, "未登录或凭证缺失", status_code=401)
    token = authorization.removeprefix("Bearer ").strip()
    try:
        payload = verify_jwt(token)
    except Exception as e:
        # 原始异常只进日志：JWT 库的错误文本可能暴露签名细节
        logger.warning(f"JWT 校验失败: {type(e).__name__}")
        raise BizException(
            BizErrorCode.UNAUTHORIZED, "登录已失效，请重新登录", status_code=401,
        ) from None

    # 已登出（jti 撤销 / 全设备失效）的 token 与过期同等对待
    await assert_token_active(payload)

    if payload.get("role") in ADMIN_ROLES:
        raise BizException(BizErrorCode.FORBIDDEN, "管理员账号不能用于患者端接口", status_code=403)

    patient_id = payload.get("patient_id")
    if patient_id is None:
        raise BizException(BizErrorCode.UNAUTHORIZED, "凭证无效", status_code=401)

    patient = await db.get(Patient, int(patient_id))
    if patient is None:
        raise BizException(BizErrorCode.UNAUTHORIZED, "用户不存在", status_code=401)
    if not getattr(patient, "enabled", True):
        raise BizException(BizErrorCode.FORBIDDEN, "账号已被禁用，请联系管理员", status_code=403)

    return {"user": _serialize(patient)}
