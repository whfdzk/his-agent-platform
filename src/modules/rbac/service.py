# src/modules/rbac/service.py
#
# RBAC 读写：角色授予关系（rbac_subject_roles）与种子数据。
#
# 分工边界：
#   - 本文件负责"库里谁有什么角色"，只在登录签发 token、授予/回收角色时被调用；
#   - 请求期鉴权只看 JWT 内已展开的 roles/scopes（src/core/permissions.py），
#     不查库，因此 RBAC 表挂了也不影响已有会话继续工作。
#
# 种子数据幂等：重复执行只补缺失项，不覆盖已改过的名称/描述。

from __future__ import annotations

from loguru import logger
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.permissions import (
    DEFAULT_ROLES,
    KIND_ADMIN,
    KIND_PATIENT,
    PERMISSION_DEFS,
    ROLE_DEFS,
    normalize_roles,
    primary_role,
    scopes_for_roles,
)
from src.modules.rbac.model import (
    RbacPermission,
    RbacRole,
    RbacRolePermission,
    RbacSubjectRole,
)

VALID_KINDS = (KIND_PATIENT, KIND_ADMIN)


class RbacError(Exception):
    """角色授予失败（非法主体类型 / 非法角色 / 角色与账号体系不匹配）。"""


def validate_kind(subject_kind: str) -> str:
    kind = str(subject_kind or "").strip()
    if kind not in VALID_KINDS:
        raise RbacError(f"非法主体类型：{subject_kind!r}（仅支持 patient/admin）")
    return kind


async def roles_of(db: AsyncSession, subject_kind: str, subject_id: int | str) -> list[str]:
    """库里查到的角色（未做合法性与默认兜底，调用方用 identity_of 拿最终结果）。"""
    kind = validate_kind(subject_kind)
    rows = await db.execute(
        select(RbacSubjectRole.role_code).where(
            RbacSubjectRole.subject_kind == kind,
            RbacSubjectRole.subject_id == int(subject_id),
        )
    )
    return [str(r) for r in rows.scalars().all()]


async def identity_of(
    db: AsyncSession, subject_kind: str, subject_id: int | str,
) -> tuple[tuple[str, ...], str, frozenset[str]]:
    """返回 (roles, 主角色, scopes)。

    库里没有记录时按该侧默认角色兜底（患者注册即得 patient，管理员默认 admin），
    保证"新账号不必先授权也能登录"。
    """
    kind = validate_kind(subject_kind)
    raw = await roles_of(db, kind, subject_id)
    roles = normalize_roles(raw, kind=kind)
    return roles, primary_role(roles, kind=kind), scopes_for_roles(roles)


async def grant_role(
    db: AsyncSession,
    subject_kind: str,
    subject_id: int | str,
    role_code: str,
    *,
    granted_by: str = "system",
    note: str = "",
    commit: bool = True,
) -> bool:
    """授予角色。已存在则返回 False（幂等），角色与账号体系不匹配则抛 RbacError。"""
    kind = validate_kind(subject_kind)
    role = str(role_code or "").strip()
    spec = ROLE_DEFS.get(role)
    if not spec:
        raise RbacError(f"未知角色：{role!r}")
    if spec["side"] != kind:
        raise RbacError(f"角色 {role} 属于 {spec['side']} 侧账号，不能授予 {kind} 账号")

    sid = int(subject_id)
    exists = (
        await db.execute(
            select(RbacSubjectRole.id).where(
                RbacSubjectRole.subject_kind == kind,
                RbacSubjectRole.subject_id == sid,
                RbacSubjectRole.role_code == role,
            ).limit(1)
        )
    ).scalar_one_or_none()
    if exists is not None:
        return False

    db.add(
        RbacSubjectRole(
            subject_kind=kind, subject_id=sid, role_code=role,
            granted_by=str(granted_by or "system")[:64], note=str(note or "")[:255],
        )
    )
    if commit:
        await db.commit()
    logger.info(f"授予角色 kind={kind} subject_id={sid} role={role} by={granted_by or 'system'}")
    return True


async def revoke_role(
    db: AsyncSession,
    subject_kind: str,
    subject_id: int | str,
    role_code: str,
    *,
    commit: bool = True,
) -> bool:
    """回收角色。返回是否真的删掉了记录。"""
    kind = validate_kind(subject_kind)
    result = await db.execute(
        delete(RbacSubjectRole).where(
            RbacSubjectRole.subject_kind == kind,
            RbacSubjectRole.subject_id == int(subject_id),
            RbacSubjectRole.role_code == str(role_code or "").strip(),
        )
    )
    if commit:
        await db.commit()
    deleted = int(result.rowcount or 0) > 0
    if deleted:
        logger.info(f"回收角色 kind={kind} subject_id={subject_id} role={role_code}")
    return deleted


async def list_grants(db: AsyncSession, subject_kind: str, subject_id: int | str) -> list[dict]:
    kind = validate_kind(subject_kind)
    rows = await db.execute(
        select(RbacSubjectRole).where(
            RbacSubjectRole.subject_kind == kind,
            RbacSubjectRole.subject_id == int(subject_id),
        ).order_by(RbacSubjectRole.id)
    )
    return [
        {
            "role_code": g.role_code,
            "granted_by": g.granted_by,
            "note": g.note,
            "created_at": g.created_at.isoformat() if g.created_at else None,
        }
        for g in rows.scalars().all()
    ]


async def seed_defaults(db: AsyncSession, *, commit: bool = True) -> dict[str, int]:
    """把 src/core/permissions.py 的静态定义同步进库（幂等）。"""
    counts = {"roles_added": 0, "permissions_added": 0, "mappings_added": 0}

    existing_roles = {
        str(c): r for c, r in (
            (row.code, row) for row in (await db.execute(select(RbacRole))).scalars().all()
        )
    }
    for code, spec in ROLE_DEFS.items():
        if code in existing_roles:
            continue
        db.add(RbacRole(code=code, name=spec["name"], side=spec["side"], description=spec["description"]))
        counts["roles_added"] += 1

    existing_perms = {
        str(p.code) for p in (await db.execute(select(RbacPermission))).scalars().all()
    }
    for code, desc in PERMISSION_DEFS.items():
        if code in existing_perms:
            continue
        db.add(RbacPermission(code=code, name=desc, description=desc))
        counts["permissions_added"] += 1

    existing_map = {
        (str(m.role_code), str(m.permission_code))
        for m in (await db.execute(select(RbacRolePermission))).scalars().all()
    }
    for role, spec in ROLE_DEFS.items():
        for perm in spec["scopes"]:
            if perm == "*" or (role, perm) in existing_map:
                continue
            db.add(RbacRolePermission(role_code=role, permission_code=perm))
            counts["mappings_added"] += 1

    if commit:
        await db.commit()
    if any(counts.values()):
        logger.info(f"RBAC 种子同步：{counts}")
    return counts


async def ensure_bootstrap_roles(db: AsyncSession) -> None:
    """启动期兜底：库里一个角色都没有时自动播种（迁移已写过则几乎无成本）。"""
    try:
        has_any = (await db.execute(select(RbacRole.id).limit(1))).scalar_one_or_none()
        if has_any is None:
            logger.warning("RBAC 角色表为空，自动播种默认角色")
            await seed_defaults(db)
    except Exception as e:  # noqa: BLE001 —— 启动期自愈失败不应阻断服务
        logger.warning(f"RBAC 启动期播种跳过：{type(e).__name__}: {e}")


async def ensure_default_grant(db: AsyncSession, subject_kind: str, subject_id: int | str) -> None:
    """新账号补默认角色（注册后调用；已有任意角色则不动）。

    不提交事务：交给调用方的会话（get_db 在请求结束时统一 commit），
    避免注册流程里"账号已提交但后续步骤失败"的半成品状态。
    """
    kind = validate_kind(subject_kind)
    existing = await roles_of(db, kind, subject_id)
    if existing:
        return
    for role in DEFAULT_ROLES.get(kind, ()):
        await grant_role(db, kind, subject_id, role, granted_by="system", note="默认授予", commit=False)
    await db.flush()
