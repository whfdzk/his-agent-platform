"""
工具权限过滤：不同角色的用户只能调用不同的工具。
借鉴 ai-commerce-interview 的实现。

为什么需要工具权限过滤：
- 患者不能调用运营数据查询工具（涉及医院内部数据）
- 普通用户不能调用管理端工具
- 工具权限要和角色绑定，不能只靠前端隐藏
"""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from loguru import logger


class UserRole(str, Enum):
    """用户角色。"""
    PATIENT = "patient"  # 普通患者
    DOCTOR = "doctor"  # 医生
    ADMIN = "admin"  # 管理员
    SUPER_ADMIN = "superadmin"  # 超级管理员


@dataclass
class ToolPermission:
    """工具权限定义。"""
    tool_name: str
    allowed_roles: set[UserRole]
    description: str = ""

    def is_allowed(self, role: UserRole) -> bool:
        """检查某个角色是否可以使用这个工具。"""
        return role in self.allowed_roles


# 工具权限注册表
_TOOL_PERMISSIONS: dict[str, ToolPermission] = {}


def register_tool_permission(
    tool_name: str,
    allowed_roles: list[UserRole],
    description: str = "",
) -> None:
    """注册工具权限。"""
    _TOOL_PERMISSIONS[tool_name] = ToolPermission(
        tool_name=tool_name,
        allowed_roles=set(allowed_roles),
        description=description,
    )
    logger.debug(f"[ToolPermission] 注册工具权限: {tool_name} -> {[r.value for r in allowed_roles]}")


def check_tool_permission(tool_name: str, user_role: UserRole) -> tuple[bool, str]:
    """检查用户是否有权限调用某个工具。

    Returns:
        (allowed, reason): 是否允许，以及原因
    """
    # 如果工具没有注册权限，默认所有角色都可以用（向后兼容）
    if tool_name not in _TOOL_PERMISSIONS:
        return True, ""

    permission = _TOOL_PERMISSIONS[tool_name]
    if permission.is_allowed(user_role):
        return True, ""

    reason = f"权限不足：工具 {tool_name} 需要角色 {[r.value for r in permission.allowed_roles]}，当前角色 {user_role.value}"
    logger.warning(f"[ToolPermission] {reason}")
    return False, reason


def filter_tools_by_role(tools: list[Any], user_role: UserRole) -> list[Any]:
    """根据用户角色过滤可用工具列表。"""
    filtered = []
    for tool in tools:
        tool_name = getattr(tool, "name", str(tool))
        allowed, _ = check_tool_permission(tool_name, user_role)
        if allowed:
            filtered.append(tool)
        else:
            logger.info(f"[ToolPermission] 过滤掉工具: {tool_name}（角色 {user_role.value} 无权限）")
    return filtered


# 注册默认的工具权限
def _register_default_permissions():
    """注册默认的工具权限配置。"""

    # 患者端工具：所有患者都可以用
    patient_tools = [
        "search_memory",
        "save_memory",
        "get_my_profile",
        "call_inquiry_agent",
        "call_report_agent",
        "call_drug_agent",
        "call_health_agent",
        "call_guide_agent",
        "call_knowledge_agent",
    ]
    for tool_name in patient_tools:
        register_tool_permission(
            tool_name=tool_name,
            allowed_roles=[UserRole.PATIENT, UserRole.DOCTOR, UserRole.ADMIN, UserRole.SUPER_ADMIN],
            description="患者端通用工具",
        )

    # 运营数据工具：只有管理员可以用
    admin_tools = [
        "call_operation_agent",
    ]
    for tool_name in admin_tools:
        register_tool_permission(
            tool_name=tool_name,
            allowed_roles=[UserRole.ADMIN, UserRole.SUPER_ADMIN],
            description="运营数据查询工具（仅限管理员）",
        )


# 模块加载时自动注册默认权限
_register_default_permissions()
