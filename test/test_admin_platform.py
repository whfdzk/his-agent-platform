# 管理端平台域的确定性单测（不依赖数据库 / 外部服务）。
#
# 覆盖点：
# 1. 所有 /api/v1/admin 路由都必须挂 get_current_admin（新增模块最容易漏守卫）；
# 2. PHI 掩码不回归（手机号）；
# 3. 系统配置的 switch / dict 序列化与前端控件语义一致；
# 4. Agent 创建入参默认值与类型白名单。

import pytest

from src.api.deps import get_current_admin
from src.api.routers import (
    admin_agents,
    admin_conversations,
    admin_membership,
    admin_prompts,
    admin_stats,
    admin_system,
    admin_users,
)
from src.api.routers.admin_agents import ALLOWED_TYPES, AgentUpsert
from src.api.routers.admin_system import _serialize as serialize_config
from src.api.routers.admin_users import _mask_phone
from src.modules.platform.model import SystemConfig

ADMIN_ROUTERS = [
    admin_users.router,
    admin_agents.router,
    admin_prompts.router,
    admin_conversations.router,
    admin_system.router,
    admin_stats.router,
    admin_membership.router,
]


@pytest.mark.parametrize("router", ADMIN_ROUTERS, ids=lambda r: r.prefix)
def test_admin_routers_are_guarded(router):
    """管理端路由前缀正确，且每个 router 都挂了管理员守卫依赖。"""
    assert router.prefix.startswith("/api/v1/admin/")
    guarded = any(
        getattr(dep, "dependency", None) is get_current_admin for dep in router.dependencies
    )
    assert guarded, f"{router.prefix} 缺少 get_current_admin 守卫"


def test_mask_phone_keeps_prefix_and_suffix():
    assert _mask_phone("13800138000") == "138****8000"


def test_mask_phone_handles_short_or_empty():
    assert _mask_phone(None) == ""
    assert _mask_phone("123") == "123"


def test_switch_config_serializes_to_bool():
    cfg = SystemConfig(
        key="chat.enabled", value="true", config_type="switch",
        group="chat", name="聊天开关", description="", enabled=True,
    )
    data = serialize_config(cfg)
    assert data["value"] is True
    assert data["raw_value"] == "true"


def test_dict_config_serializes_to_list():
    cfg = SystemConfig(
        key="dict.department", value='["内科","外科"]', config_type="dict",
        group="general", name="科室字典", description="", enabled=True,
    )
    assert serialize_config(cfg)["value"] == ["内科", "外科"]


def test_dict_config_with_broken_json_falls_back_to_empty_list():
    cfg = SystemConfig(
        key="dict.broken", value="not-json", config_type="dict",
        group="general", name="坏字典", description="", enabled=True,
    )
    assert serialize_config(cfg)["value"] == []


def test_param_config_keeps_raw_string():
    cfg = SystemConfig(
        key="chat.max_turns", value="12", config_type="param",
        group="chat", name="最大轮次", description="", enabled=True,
    )
    assert serialize_config(cfg)["value"] == "12"


def test_agent_upsert_defaults():
    """新建 Agent 只需 code + name，其余字段有安全默认值（前端表单最小化提交）。"""
    req = AgentUpsert(code="inquiry", name="问诊 Agent")
    assert req.agent_type == "worker"
    assert req.description == ""
    assert req.config is None


def test_agent_type_whitelist():
    assert ALLOWED_TYPES == {"supervisor", "worker"}
