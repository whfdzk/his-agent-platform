# Agent 配置热加载的确定性单测（不连数据库 / 不调 LLM）。
#
# 覆盖点：
# 1. 读库失败时保留上一版配置继续服务，并记录 last_error（不能把缓存清空）；
# 2. 装配期发现致命问题（未知工具 / 未知子 Agent）时整条配置被拒，回退内置默认；
# 3. 指纹未变不递增版本；只换 prompt 正文（revision 不变）也必须算变更；
# 4. 并发刷新被串行化，不会交错覆盖缓存；
# 5. TTL 内不重复读库；
# 6. runtime_status 暴露版本 / 生效时间 / 被拒配置。

import asyncio

import pytest

from src.core import agent_registry as registry
from src.core.agent_registry import (
    AgentLoadResult,
    AgentRuntimeConfig,
    ConfigIssue,
    get_active_config,
)


@pytest.fixture(autouse=True)
def _clean_registry():
    registry.reset()
    yield
    registry.reset()


# ── 失败保留 ─────────────────────────────────────────────────────────────


async def test_provider_failure_keeps_last_good_config():
    async def ok():
        return [AgentRuntimeConfig(code="drug", status="published", temperature=0.2)]

    registry.set_provider(ok)
    first = await registry.refresh_agents(reason="test")
    assert first.ok and first.count == 1
    version = first.version

    async def boom():
        raise RuntimeError("db down")

    registry.set_provider(boom)
    second = await registry.refresh_agents(reason="test")

    assert second.ok is False
    assert "db down" in second.error
    # 旧配置继续服务，版本不动
    assert get_active_config("drug").temperature == 0.2
    assert registry.version() == version
    assert "db down" in (registry.runtime_status()["last_error"] or "")


async def test_success_after_failure_clears_last_error():
    async def boom():
        raise RuntimeError("db down")

    registry.set_provider(boom)
    await registry.refresh_agents(reason="test")

    async def ok():
        return [AgentRuntimeConfig(code="drug", status="published")]

    registry.set_provider(ok)
    result = await registry.refresh_agents(reason="test")
    assert result.ok is True
    assert registry.runtime_status()["last_error"] is None


# ── 装配期校验 ───────────────────────────────────────────────────────────


async def test_fatal_issue_rejects_whole_agent_and_falls_back():
    """未知工具不是"忽略这一个"，而是整条拒掉 —— 避免静默的能力缩水。"""

    async def bad():
        return AgentLoadResult(
            configs=(
                AgentRuntimeConfig(
                    code="knowledge", status="published", tool_ids=("nope_tool",),
                ),
            ),
            issues=(
                ConfigIssue("knowledge", "tool_ids", "引用了未知工具：nope_tool", fatal=True),
            ),
        )

    registry.set_provider(bad)
    result = await registry.refresh_agents(reason="test")

    assert result.ok is True
    assert [i.code for i in result.rejected] == ["knowledge"]
    # 被拒 → 不进缓存 → get_active_config 返回空壳（装配时用代码内置默认）
    assert get_active_config("knowledge").is_active is False
    assert get_active_config("knowledge").tool_ids == ()
    assert "knowledge" not in registry.all_agent_configs()


async def test_non_fatal_issue_keeps_agent_active():
    """提示词引用失效只是降级到内联提示词，不该让整个 Agent 下线。"""

    async def degraded():
        return AgentLoadResult(
            configs=(
                AgentRuntimeConfig(code="drug", status="published", prompt_id=99),
            ),
            issues=(
                ConfigIssue("drug", "prompt_id", "引用的提示词 99 不存在，已回退内联提示词"),
            ),
        )

    registry.set_provider(degraded)
    result = await registry.refresh_agents(reason="test")

    assert result.rejected == ()
    assert get_active_config("drug").is_active is True
    assert get_active_config("drug").prompt_id == 99


# ── 变更检测 ─────────────────────────────────────────────────────────────


async def test_unchanged_content_does_not_bump_version():
    cfg = AgentRuntimeConfig(code="drug", status="published", temperature=0.1)

    async def same():
        return [cfg]

    registry.set_provider(same)
    first = await registry.refresh_agents(reason="test")
    second = await registry.refresh_agents(reason="test")

    assert first.changed is True
    assert second.changed is False
    assert second.version == first.version


async def test_prompt_change_detected_even_if_revision_unchanged():
    """切 prompts.active_version 不碰 agents.updated_at，指纹必须看内容。"""
    base = {"code": "knowledge", "status": "published", "revision": "2026-11-01T00:00:00"}

    async def old():
        return [AgentRuntimeConfig(system_prompt="旧提示词", **base)]

    registry.set_provider(old)
    first = await registry.refresh_agents(reason="test")

    async def new():
        return [AgentRuntimeConfig(system_prompt="新提示词", **base)]

    registry.set_provider(new)
    second = await registry.refresh_agents(reason="test")

    assert second.changed is True
    assert second.version == first.version + 1
    assert get_active_config("knowledge").system_prompt == "新提示词"


# ── 并发与 TTL ───────────────────────────────────────────────────────────


async def test_concurrent_refresh_is_serialized():
    """refresh 是异步临界区：并发进来必须排队，不能交错覆盖缓存。"""
    active = 0
    peak = 0
    calls = 0

    async def slow():
        nonlocal active, peak, calls
        calls += 1
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return [AgentRuntimeConfig(code="drug", status="published")]

    registry.set_provider(slow)
    results = await asyncio.gather(*(registry.refresh_agents(reason="test") for _ in range(5)))

    assert all(r.ok for r in results)
    assert calls == 5
    assert peak == 1
    assert get_active_config("drug").is_active is True


async def test_maybe_refresh_skips_within_ttl():
    calls = 0

    async def counting():
        nonlocal calls
        calls += 1
        return [AgentRuntimeConfig(code="drug", status="published")]

    registry.set_provider(counting)
    assert await registry.maybe_refresh(interval=10) is True   # 缓存空 → 必刷
    assert calls == 1
    assert await registry.maybe_refresh(interval=10) is False  # TTL 内 → 不读库
    assert calls == 1
    assert await registry.maybe_refresh(interval=0) is True    # 0 = 不做 TTL 判断
    assert calls == 2


async def test_watcher_disabled_when_interval_is_zero():
    async def provider():
        return [AgentRuntimeConfig(code="drug", status="published")]

    registry.set_provider(provider)
    assert await registry.start_watcher(0) is False


# ── 状态观测 ─────────────────────────────────────────────────────────────


async def test_runtime_status_reports_version_rejections_and_sources():
    async def mixed():
        return AgentLoadResult(
            configs=(
                AgentRuntimeConfig(code="drug", status="published"),
                AgentRuntimeConfig(code="supervisor", status="published", sub_agent_codes=("ghost",)),
            ),
            issues=(
                ConfigIssue("supervisor", "sub_agent_codes", "引用了未知子 Agent：ghost", fatal=True),
            ),
        )

    registry.set_provider(mixed)
    await registry.refresh_agents(reason="test")
    status = registry.runtime_status()

    assert status["version"] >= 1
    assert status["loaded_at"]
    assert status["last_reason"] == "test"
    assert [a["code"] for a in status["agents"]] == ["drug"]
    assert status["agents"][0]["config_source"] == "database"
    assert status["rejected"][0]["code"] == "supervisor"
    assert status["rejected"][0]["field"] == "sub_agent_codes"


async def test_describe_message_reflects_reload_outcome():
    ok = registry.RefreshResult(ok=True, version=3, changed=True, count=1)
    assert "已生效 v3" in ok.describe("发布成功")

    unchanged = registry.RefreshResult(ok=True, version=3, changed=False, count=1)
    assert "无变化" in unchanged.describe("更新成功")

    failed = registry.RefreshResult(ok=False, version=3, error="db down")
    assert "重载失败" in failed.describe("发布成功")

    rejected = registry.RefreshResult(
        ok=True, version=4, changed=True, count=1,
        rejected=(ConfigIssue("knowledge", "tool_ids", "未知工具", fatal=True),),
    )
    assert "knowledge" in rejected.describe("发布成功")
