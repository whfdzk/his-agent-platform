# Agent 配置化改造的确定性单测（不连数据库 / 不调 LLM）。
#
# 覆盖点：
# 1. agents.config(JSONB) 的解析容错与默认值（脏数据不能让 Agent 建不起来）；
# 2. 「已发布才生效」的语义：草稿/停用回退代码内置默认；
# 3. 配置缓存刷新 + Agent 单例按配置指纹自动重建；
# 4. 管理端保存前的配置校验（未知工具 / 越界温度 → 400）；
# 5. 工具注册表与工具目录的一致性（工具 id 不会因为手写副本而漏配）。

import pytest

from src.agents.tool_registry import (
    TOOL_CATEGORIES,
    WORKER_TOOL_IDS,
    reset_tool_cache,
    validate_sub_agent_codes,
    validate_tool_ids,
)
from src.core import agent_registry as registry
from src.core.agent_registry import (
    AgentRuntimeConfig,
    SummarizationConfig,
    get_active_config,
    parse_agent_config,
)
from src.core.exceptions import BizException
from src.modules.platform.agent_provider import builtin_agent_config


@pytest.fixture(autouse=True)
def _clean_registry():
    registry.reset()
    reset_tool_cache()
    yield
    registry.reset()
    reset_tool_cache()


# ── 配置解析 ─────────────────────────────────────────────────────────────


def test_parse_config_falls_back_to_defaults_for_empty_payload():
    cfg = parse_agent_config("report", None, name="报告解读", status="published")
    assert cfg.code == "report"
    assert cfg.name == "报告解读"
    assert cfg.is_active is True
    assert cfg.temperature is None  # None = 用 worker 代码里的默认温度
    assert cfg.tool_ids == ()
    assert cfg.summarization == SummarizationConfig()


def test_parse_config_ignores_broken_types():
    """一条脏配置（类型全错）只能退化，不能抛异常。"""
    cfg = parse_agent_config(
        "drug",
        {
            "tool_ids": "query_drug_info",  # 应该是数组
            "temperature": "hot",
            "max_turns": -3,
            "sub_agent_codes": [None, "drug", ""],
            "summarization": {"trigger_tokens": "many", "keep_messages": 4},
        },
    )
    assert cfg.tool_ids == ()
    assert cfg.temperature is None
    assert cfg.max_turns == 0
    assert cfg.sub_agent_codes == ("drug",)
    # 单个字段坏了只影响那个字段
    assert cfg.summarization.trigger_tokens == 4000
    assert cfg.summarization.keep_messages == 4


def test_prompt_active_version_content_overrides_inline_prompt():
    cfg = parse_agent_config(
        "knowledge",
        {"prompt_id": 7, "system_prompt": "内联文本"},
        prompt_content="激活版本正文",
    )
    assert cfg.prompt_id == 7
    assert cfg.system_prompt == "激活版本正文"


def test_inline_prompt_used_when_prompt_not_activated():
    cfg = parse_agent_config("knowledge", {"prompt_id": 7, "system_prompt": "内联文本"})
    assert cfg.system_prompt == "内联文本"


def test_model_name_prefers_config_override_then_model_config():
    assert parse_agent_config("x", {"model": "deepseek-chat"}, model_name="qwen").model == "deepseek-chat"
    assert parse_agent_config("x", {}, model_name="qwen").model == "qwen"


# ── 生效语义 ─────────────────────────────────────────────────────────────


def test_only_published_agents_use_database_config():
    draft = AgentRuntimeConfig(code="drug", status="draft", temperature=0.1, tool_ids=("a",))
    assert draft.is_active is False
    assert draft.fallback() == AgentRuntimeConfig(code="drug")


async def test_get_active_config_returns_empty_stub_for_draft():
    async def provider():
        return [
            AgentRuntimeConfig(code="drug", status="published", temperature=0.2),
            AgentRuntimeConfig(code="report", status="draft", temperature=0.9),
        ]

    registry.set_provider(provider)
    await registry.refresh_agents()

    assert get_active_config("drug").temperature == 0.2
    # 草稿不改线上行为
    assert get_active_config("report").temperature is None
    # 库里没有的 code 也不炸
    assert get_active_config("unknown").code == "unknown"


async def test_refresh_failure_keeps_last_known_good_config():
    async def ok():
        return [AgentRuntimeConfig(code="drug", status="published", temperature=0.2)]

    registry.set_provider(ok)
    await registry.refresh_agents()

    async def boom():
        raise RuntimeError("db down")

    registry.set_provider(boom)
    await registry.refresh_agents()
    assert get_active_config("drug").temperature == 0.2


# ── 单例缓存 ─────────────────────────────────────────────────────────────


def test_runtime_agent_rebuilds_only_when_config_changes():
    from src.agents.builder import get_runtime_agent, reset_agent_cache

    builds: list[AgentRuntimeConfig] = []

    def build(cfg: AgentRuntimeConfig) -> object:
        builds.append(cfg)
        return object()

    registry._cache = {"report": AgentRuntimeConfig(code="report", status="published")}
    first = get_runtime_agent("report", build)
    assert get_runtime_agent("report", build) is first
    assert len(builds) == 1

    # 模拟管理端保存后刷新：配置对象变化 → 重建
    registry._cache = {
        "report": AgentRuntimeConfig(code="report", status="published", temperature=0.5),
    }
    second = get_runtime_agent("report", build)
    assert second is not first
    assert len(builds) == 2
    reset_agent_cache()


# ── 管理端校验 ───────────────────────────────────────────────────────────


def test_normalize_config_rejects_unknown_tool():
    from src.api.routers.admin_agents import normalize_agent_config

    with pytest.raises(BizException) as exc:
        normalize_agent_config({"tool_ids": ["get_recent_reports", "not_a_real_tool"]})
    assert "not_a_real_tool" in str(exc.value)


def test_normalize_config_rejects_out_of_range_temperature():
    from src.api.routers.admin_agents import normalize_agent_config

    with pytest.raises(BizException):
        normalize_agent_config({"temperature": 9})


def test_normalize_config_dedupes_and_keeps_unknown_keys():
    from src.api.routers.admin_agents import normalize_agent_config

    data = normalize_agent_config({
        "tool_ids": ["save_memory", "save_memory"],
        "sub_agent_codes": ["drug"],
        "future_flag": True,
    })
    assert data["tool_ids"] == ["save_memory"]
    assert data["sub_agent_codes"] == ["drug"]
    assert data["future_flag"] is True
    assert data["summarization"]["trigger_tokens"] == 4000


def test_normalize_config_accepts_legacy_minimal_payload():
    """老前端只传 4 个 key，升级后仍然能保存。"""
    from src.api.routers.admin_agents import normalize_agent_config

    data = normalize_agent_config({
        "tool_ids": [], "skill_ids": [], "prompt_id": None, "model_config_id": None,
    })
    assert data["prompt_id"] is None
    assert data["max_turns"] == 0
    assert data["system_prompt"] == ""


# ── 工具注册表 ───────────────────────────────────────────────────────────


def test_every_worker_tool_is_categorized():
    missing = set(WORKER_TOOL_IDS.values()) - set(TOOL_CATEGORIES)
    assert not missing, f"这些调度工具缺少分类：{missing}"


def test_validate_helpers_report_unknown_entries():
    assert validate_tool_ids(["save_memory", "nope"]) == ["nope"]
    assert validate_sub_agent_codes(["drug", "nope"]) == ["nope"]


def test_builtin_agent_config_shape():
    config = builtin_agent_config({
        "system_prompt": "你是助手",
        "temperature": 0.1,
        "tool_ids": ["save_memory"],
        "sub_agent_codes": ["drug"],
        "degrade_message": "稍后再试",
    })
    assert config["system_prompt"] == "你是助手"
    assert config["tool_ids"] == ["save_memory"]
    assert config["sub_agent_codes"] == ["drug"]
    assert config["prompt_id"] is None
    assert "summarization" not in config
