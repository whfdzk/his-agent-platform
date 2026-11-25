# Harness 层：控制层门面 + 受控 Runtime 网关的确定性单测。
#
# 不依赖真实 Redis / LLM：网关用假的 awaitable 驱动，facade 用 monkeypatch 替换下层的取配置与投递。

import asyncio

import pytest
from langchain_core.messages import ToolMessage

from src.harness import policy
from src.harness.core import AgentHarness
from src.harness.runtime import ToolRuntimeGateway
from src.agents.tool_runtime_middleware import ToolRuntimeMiddleware


# ── policy ──────────────────────────────────────────────────────────────
def test_policy_allows_normal_call():
    d = policy.check_tool_call("query_drug_info", {"drug_name": "阿司匹林"})
    assert d.allowed


@pytest.mark.parametrize("name,args,code", [
    ("", {}, "empty_tool"),
    ("shell", {"cmd": "ls"}, "tool_blocked"),
    ("query_drug_info", ["不是对象"], "bad_args"),
    ("query_drug_info", {"q": "x" * 5000}, "args_too_large"),
])
def test_policy_denies(name, args, code):
    d = policy.check_tool_call(name, args)
    assert not d.allowed
    assert d.code == code


# ── runtime 网关 ────────────────────────────────────────────────────────
async def test_gateway_ok_and_stats():
    gw = ToolRuntimeGateway(timeout_s=5, max_attempts=2)

    async def call():
        return "ok-result"

    obs = await gw.execute("query_drug_info", {"drug_name": "x"}, call)
    assert obs.ok and obs.result == "ok-result" and obs.attempts == 1
    stats = gw.stats()
    assert stats["calls"] == 1 and stats["ok"] == 1


async def test_gateway_retries_read_only_on_connection_error():
    gw = ToolRuntimeGateway(timeout_s=5, max_attempts=2)
    attempts = {"n": 0}

    async def flaky():
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise ConnectionError("Redis/MCP 连接被重置")
        return "ok"

    obs = await gw.execute("query_drug_info", {"drug_name": "x"}, flaky)
    assert obs.ok and obs.attempts == 2
    assert gw.stats()["retries"] == 1


async def test_gateway_does_not_retry_timeout():
    """超时不重试：已经吃满一次预算，重试会把尾延迟翻倍。"""
    gw = ToolRuntimeGateway(timeout_s=0.05, max_attempts=2)

    async def slow():
        await asyncio.sleep(0.2)

    obs = await gw.execute("query_drug_info", {"drug_name": "x"}, slow)
    assert not obs.ok and obs.timed_out and obs.attempts == 1
    assert gw.stats()["retries"] == 0


async def test_gateway_never_retries_write_tool():
    gw = ToolRuntimeGateway(timeout_s=0.05, max_attempts=2)

    async def slow():
        await asyncio.sleep(0.2)

    obs = await gw.execute("create_appointment", {"doctor_id": 1}, slow)
    assert not obs.ok and obs.timed_out and obs.attempts == 1
    assert gw.stats()["retries"] == 0


async def test_gateway_denied_by_policy_never_calls_tool():
    gw = ToolRuntimeGateway(timeout_s=5, max_attempts=2)
    called = {"n": 0}

    async def call():
        called["n"] += 1
        return "should-not-happen"

    obs = await gw.execute("shell", {"cmd": "ls"}, call)
    assert obs.denied and not obs.ok
    assert called["n"] == 0
    assert gw.stats()["denied"] == 1


async def test_gateway_exception_is_normalized_for_model():
    gw = ToolRuntimeGateway(timeout_s=5, max_attempts=2)

    async def boom():
        raise ValueError("药品库连接串错误")

    obs = await gw.execute("query_drug_info", {"drug_name": "x"}, boom)
    assert not obs.ok and obs.error_type == "ValueError"
    text = obs.as_error_text()
    assert "工具执行失败" in text and "不要重复调用" in text


# ── 中间件 ──────────────────────────────────────────────────────────────
class _FakeRequest:
    def __init__(self, name, args=None, call_id="c1"):
        self.tool_call = {"name": name, "args": args or {}, "id": call_id}


async def test_middleware_returns_tool_message_on_failure():
    gw = ToolRuntimeGateway(timeout_s=0.05, max_attempts=1)
    mw = ToolRuntimeMiddleware(gateway=gw)

    async def handler(_req):
        await asyncio.sleep(0.2)

    result = await mw.awrap_tool_call(_FakeRequest("query_drug_info"), handler)
    assert isinstance(result, ToolMessage)
    assert result.status == "error"
    assert "工具执行超时" in str(result.content)


async def test_middleware_passes_handoff_directly():
    gw = ToolRuntimeGateway(timeout_s=0.01, max_attempts=1)
    mw = ToolRuntimeMiddleware(gateway=gw)
    seen = {"called": False}

    async def handler(_req):
        seen["called"] = True
        return ToolMessage(content="interrupted", tool_call_id="c1")

    result = await mw.awrap_tool_call(_FakeRequest("request_human_handoff"), handler)
    assert seen["called"] and result.content == "interrupted"
    assert gw.stats()["calls"] == 0


async def test_middleware_passthrough_success():
    gw = ToolRuntimeGateway(timeout_s=5, max_attempts=1)
    mw = ToolRuntimeMiddleware(gateway=gw)

    async def handler(_req):
        return ToolMessage(content="药品说明：...", tool_call_id="c1")

    result = await mw.awrap_tool_call(_FakeRequest("query_drug_info"), handler)
    assert isinstance(result, ToolMessage) and result.status != "error"


# ── 控制层门面 ───────────────────────────────────────────────────────────
def test_budget_uses_default_when_max_turns_zero(monkeypatch):
    from src.core import agent_registry

    class _Cfg:
        max_turns = 0

    monkeypatch.setattr(agent_registry, "get_active_config", lambda code: _Cfg())
    budget = AgentHarness().budget("inquiry_agent")
    assert budget.max_turns == 8
    assert budget.recursion_limit == 16
    assert budget.tool_max_calls > 0


def test_budget_respects_configured_max_turns(monkeypatch):
    from src.core import agent_registry

    class _Cfg:
        max_turns = 5

    monkeypatch.setattr(agent_registry, "get_active_config", lambda code: _Cfg())
    assert AgentHarness().budget("drug_agent").recursion_limit == 10


async def test_harness_emit_delegates_with_tenant(monkeypatch):
    from src.infra import event_bus

    captured = {}

    async def fake_publish(event_type, payload, *, dedupe_key="", tenant_id=1):
        captured.update({"type": event_type, "payload": payload,
                         "dedupe_key": dedupe_key, "tenant_id": tenant_id})
        return "evt-1"

    monkeypatch.setattr(event_bus, "publish", fake_publish)
    result = await AgentHarness(tenant_id=7).emit("handoff.ticket.created", {"a": 1}, dedupe_key="T1")
    assert result == "evt-1"
    assert captured["tenant_id"] == 7 and captured["dedupe_key"] == "T1"
