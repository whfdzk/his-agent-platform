# src/core/permissions.py
#
# 角色与权限码（RBAC）静态定义 —— 纯逻辑，不连数据库。
#
# 设计取舍（B1 最小闭环）：
# - 角色 -> 权限（scope）的映射是**静态表**，签发 token 时展开成 scopes 写进 JWT，
#   请求期直接读 payload，不再 join 三张表。原因是本系统鉴权在每次请求的热路径上，
#   多一次 3 表 join 换来的灵活性，在"角色集合很小且几乎不变"的场景下不划算。
# - 数据库里的 rbac_* 表只回答"谁被授予了哪个角色"，不参与每次请求的判定。
# - 角色变更有延迟（旧 token 内的 scopes 不会自动更新），靠 token 有效期（默认 30 分钟）
#   + 撤销名单（token_revocation）兜底；紧急撤权走"登出全部设备"。
#
# scope 命名统一为 "资源:动作"，便于后续按前缀批量授权（如 knowledge:*）。

from __future__ import annotations

from typing import Iterable

# ---------------- 主体类型 ----------------
KIND_PATIENT = "patient"   # 患者端账号（patients 表）
KIND_ADMIN = "admin"       # 管理端账号（admins 表）

# ---------------- 角色码 ----------------
ROLE_PATIENT = "patient"
ROLE_CLINICIAN = "clinician"
ROLE_NURSE = "nurse"
# 坐席（人工接管）：院内客服/导诊账号，负责接工单、回消息、转诊建议。
# 不授予 appointment:approve —— 坐席能安抚与引导，但代批高危动作必须由医生做。
ROLE_SERVICE = "service"
ROLE_ADMIN = "admin"
ROLE_SUPERADMIN = "superadmin"

# 通配符：持有它表示拥有全部权限（仅 superadmin）
WILDCARD = "*"

# ---------------- 权限码 ----------------
# 值是人类可读说明，键是写进 token 的 scope 字符串
PERMISSION_DEFS: dict[str, str] = {
    # 问诊 / 会话
    "chat:read": "查看问诊会话与历史消息",
    "chat:write": "发起问诊、写入会话",
    # 预约
    "appointment:read": "查看预约记录",
    "appointment:write": "创建/取消本人预约",
    "appointment:approve": "审批他人预约与高危动作确认",
    # 患者档案
    "patient:read": "查看患者档案（医护侧）",
    "patient:write": "修改患者档案",
    # 报告
    "report:read": "查看健康报告",
    "report:write": "生成健康报告",
    # 审计
    "audit:read": "查看审计日志、引用与轨迹",
    # 知识库
    "knowledge:read": "查看知识库文档与检索结果",
    "knowledge:write": "上传/删除知识库文档",
    # 模型配置
    "model:write": "修改模型与提示词配置",
    # 管理端
    "admin:read": "查看管理端统计与系统状态",
    "admin:write": "修改管理端配置",
    "rbac:manage": "授予/回收角色",
    # 人工接管（C3）
    "handoff:read": "查看人工接管工单",
    "handoff:write": "认领工单、回复患者、关闭工单",
}

# ---------------- 角色定义 ----------------
# side 决定该角色属于哪一侧账号体系：
#   patient -> 可授予 patients 账号（普通患者、被授予医护权限的院内账号）
#   admin   -> 可授予 admins 账号
ROLE_DEFS: dict[str, dict] = {
    ROLE_PATIENT: {
        "name": "患者",
        "side": KIND_PATIENT,
        "description": "默认角色，注册即授予；只能操作本人数据",
        "scopes": [
            "chat:read", "chat:write",
            "appointment:read", "appointment:write",
            "patient:read", "patient:write",
            "report:read", "report:write",
        ],
    },
    ROLE_CLINICIAN: {
        "name": "医生",
        "side": KIND_PATIENT,
        "description": "医护侧：可查看患者档案与会话、审批高危动作（B2 起生效）",
        "scopes": [
            "chat:read", "chat:write",
            "appointment:read", "appointment:approve",
            "patient:read",
            "report:read",
            "audit:read",
            "knowledge:read",
            "handoff:read", "handoff:write",
        ],
    },
    ROLE_SERVICE: {
        "name": "坐席",
        "side": KIND_PATIENT,
        "description": "人工接管：接工单、回消息、给就诊引导，不做代批",
        "scopes": [
            "chat:read", "chat:write",
            "appointment:read",
            "patient:read",
            "handoff:read", "handoff:write",
        ],
    },
    ROLE_NURSE: {
        "name": "护士",
        "side": KIND_PATIENT,
        "description": "护理侧：只看患者档案与预约，不能改档案",
        "scopes": [
            "chat:read",
            "appointment:read",
            "patient:read",
        ],
    },
    ROLE_ADMIN: {
        "name": "管理员",
        "side": KIND_ADMIN,
        "description": "平台运营：知识库、模型配置、统计与审计",
        "scopes": [
            "admin:read", "admin:write",
            "knowledge:read", "knowledge:write",
            "model:write",
            "audit:read",
            "patient:read",
            "handoff:read",
        ],
    },
    ROLE_SUPERADMIN: {
        "name": "超级管理员",
        "side": KIND_ADMIN,
        "description": "全部权限，含角色授予",
        "scopes": [WILDCARD],
    },
}

# 各侧账号的默认角色（登录时若库里没有角色记录，按此兜底）
DEFAULT_ROLES: dict[str, tuple[str, ...]] = {
    KIND_PATIENT: (ROLE_PATIENT,),
    KIND_ADMIN: (ROLE_ADMIN,),
}


def normalize_roles(roles: Iterable[str] | None, *, kind: str) -> tuple[str, ...]:
    """过滤非法/不存在/不属于该侧的角色，去重并保序；空集合回落默认角色。"""
    allowed = {code for code, spec in ROLE_DEFS.items() if spec["side"] == kind}
    cleaned: list[str] = []
    for role in roles or ():
        code = str(role or "").strip()
        if code and code in allowed and code not in cleaned:
            cleaned.append(code)
    if cleaned:
        return tuple(cleaned)
    return DEFAULT_ROLES.get(kind, ())


def scopes_for_roles(roles: Iterable[str] | None) -> frozenset[str]:
    """角色集合 -> 权限集合。任一带通配符则整体为通配（减少后续判定分支）。"""
    scopes: set[str] = set()
    for role in roles or ():
        spec = ROLE_DEFS.get(str(role))
        if not spec:
            continue
        scopes.update(spec["scopes"])
    if WILDCARD in scopes:
        return frozenset({WILDCARD})
    return frozenset(scopes)


def primary_role(roles: Iterable[str] | None, *, kind: str) -> str:
    """取主角色用于 JWT 的 role 字段（兼容既有 payload.get("role") 判定）。

    管理侧优先返回 admin/superadmin，患者侧优先返回 patient —— 保持旧代码
    `payload.get("role") == "admin"` 的语义不变。
    """
    normalized = normalize_roles(roles, kind=kind)
    preferred = ROLE_ADMIN if kind == KIND_ADMIN else ROLE_PATIENT
    if preferred in normalized:
        return preferred
    return normalized[0] if normalized else preferred
