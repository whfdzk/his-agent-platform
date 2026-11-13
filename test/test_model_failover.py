# 模型故障转移的确定性单测。
#
# 目标：主模型挂掉时不能把整轮对话拖死（此前超时就是这么来的），
# 必须自动换到候选模型；全挂时才把最后一个异常抛给上层。

import pytest

from langchain_openai import ChatOpenAI

from src.core import config, model_registry
from src.core.config import FallbackChatModel
from src.core.model_registry import ModelCandidate

MAIN = "db-main-model"     # 管理端配的主模型（会失败）
BACKUP = "db-backup-model"  # 管理端配的候选
ENV = "env-model"           # .env 兜底（FallbackChatModel 自身）


@pytest.fixture(autouse=True)
def _clean_registry():
    model_registry.reset()
    yield
    model_registry.reset()


async def _provider(*candidates: ModelCandidate):
    async def _load():
        return list(candidates)
    return _load


def _candidate(name: str, model_name: str) -> ModelCandidate:
    # api_key 必填：留空会走 _resolve_chat_credentials 去读 .env，测试环境未必有
    return ModelCandidate(name=name, model_name=model_name, api_key="test-key")


def _make_llm(**kwargs) -> FallbackChatModel:
    return FallbackChatModel(model=ENV, api_key="test-key", temperature=0.0, **kwargs)


# ── 候选链注册表 ────────────────────────────────────────────────────────

async def test_registry_returns_empty_chain_without_provider():
    assert await model_registry.get_candidates() == []


async def test_registry_refresh_uses_injected_provider():
    model_registry.set_provider(await _provider(_candidate("a", MAIN)))
    chain = await model_registry.refresh_candidates()
    assert [c.model_name for c in chain] == [MAIN]


async def test_registry_keeps_last_chain_when_provider_fails():
    """读库失败不能清空缓存：否则一次 DB 抖动就把故障转移能力整没了。"""
    model_registry.set_provider(await _provider(_candidate("a", MAIN)))
    await model_registry.refresh_candidates()

    async def _boom():
        raise RuntimeError("db down")

    model_registry.set_provider(_boom)
    assert [c.model_name for c in await model_registry.refresh_candidates()] == [MAIN]


async def test_registry_refreshes_when_cache_is_stale():
    model_registry.set_provider(await _provider(_candidate("a", MAIN)))
    await model_registry.refresh_candidates()

    model_registry.set_provider(await _provider(_candidate("a", MAIN), _candidate("b", BACKUP)))
    # 间隔设为 0 → 缓存一定算过期
    chain = await model_registry.get_candidates(refresh_interval=0)
    assert [c.model_name for c in chain] == [MAIN, BACKUP]


def test_registry_version_increments_on_change():
    model_registry.reset()
    assert model_registry.version() == 0


# ── 非流式故障转移 ──────────────────────────────────────────────────────

async def test_agenerate_switches_to_next_candidate(monkeypatch):
    calls: list[str] = []

    async def _fake(self, messages, stop=None, run_manager=None, **kwargs):
        calls.append(self.model_name)
        if self.model_name == MAIN:
            raise RuntimeError("gateway 502")
        return "OK"

    monkeypatch.setattr(ChatOpenAI, "_agenerate", _fake)
    model_registry.set_provider(await _provider(_candidate("main", MAIN)))
    await model_registry.refresh_candidates()

    assert await _make_llm()._agenerate([]) == "OK"
    assert calls == [MAIN, ENV]  # 主模型失败 → 回退到 .env 兜底


async def test_agenerate_tries_candidates_in_order(monkeypatch):
    calls: list[str] = []

    async def _fake(self, messages, stop=None, run_manager=None, **kwargs):
        calls.append(self.model_name)
        if self.model_name in (MAIN, BACKUP):
            raise RuntimeError("down")
        return "OK"

    monkeypatch.setattr(ChatOpenAI, "_agenerate", _fake)
    model_registry.set_provider(await _provider(_candidate("main", MAIN), _candidate("backup", BACKUP)))
    await model_registry.refresh_candidates()

    assert await _make_llm()._agenerate([]) == "OK"
    assert calls == [MAIN, BACKUP, ENV]  # 严格按 priority 顺序，不是随机挑


async def test_agenerate_raises_last_error_when_all_candidates_fail(monkeypatch):
    calls: list[str] = []

    async def _fake(self, messages, stop=None, run_manager=None, **kwargs):
        calls.append(self.model_name)
        raise RuntimeError(f"{self.model_name} down")

    monkeypatch.setattr(ChatOpenAI, "_agenerate", _fake)
    model_registry.set_provider(await _provider(_candidate("main", MAIN)))
    await model_registry.refresh_candidates()

    with pytest.raises(RuntimeError, match="env-model down"):
        await _make_llm()._agenerate([])
    assert calls == [MAIN, ENV]  # 每个候选都试过，没有提前放弃


async def test_agenerate_uses_only_self_when_chain_is_empty(monkeypatch):
    calls: list[str] = []

    async def _fake(self, messages, stop=None, run_manager=None, **kwargs):
        calls.append(self.model_name)
        return "OK"

    monkeypatch.setattr(ChatOpenAI, "_agenerate", _fake)
    assert await _make_llm()._agenerate([]) == "OK"
    assert calls == [ENV]  # 没配候选时行为与改造前一致


async def test_agenerate_skips_fallbacks_when_disabled(monkeypatch):
    calls: list[str] = []

    async def _fake(self, messages, stop=None, run_manager=None, **kwargs):
        calls.append(self.model_name)
        raise RuntimeError("down")

    monkeypatch.setattr(ChatOpenAI, "_agenerate", _fake)
    monkeypatch.setattr(config.get_settings(), "MODEL_FAILOVER_ENABLED", False, raising=False)
    model_registry.set_provider(await _provider(_candidate("main", MAIN)))
    await model_registry.refresh_candidates()

    with pytest.raises(RuntimeError):
        await _make_llm()._agenerate([])
    assert calls == [ENV]  # 开关关闭 → 完全不碰 DB 里的候选


def test_generate_switches_to_next_candidate(monkeypatch):
    calls: list[str] = []

    def _fake(self, messages, stop=None, run_manager=None, **kwargs):
        calls.append(self.model_name)
        if self.model_name == MAIN:
            raise RuntimeError("down")
        return "OK"

    monkeypatch.setattr(ChatOpenAI, "_generate", _fake)
    import asyncio
    model_registry.set_provider(asyncio.run(_provider(_candidate("main", MAIN))))
    asyncio.run(model_registry.refresh_candidates())

    assert _make_llm()._generate([]) == "OK"
    assert calls == [MAIN, ENV]


# ── 流式故障转移 ────────────────────────────────────────────────────────

async def test_astream_switches_before_first_chunk(monkeypatch):
    calls: list[str] = []

    async def _fake(self, *args, **kwargs):
        calls.append(self.model_name)
        if self.model_name == MAIN:
            raise RuntimeError("connect error")  # 首个 chunk 之前就失败
        yield "chunk-1"
        yield "chunk-2"

    monkeypatch.setattr(ChatOpenAI, "_astream", _fake)
    model_registry.set_provider(await _provider(_candidate("main", MAIN)))
    await model_registry.refresh_candidates()

    chunks = [c async for c in _make_llm()._astream([])]
    assert chunks == ["chunk-1", "chunk-2"]
    assert calls == [MAIN, ENV]


async def test_astream_does_not_retry_after_emitting(monkeypatch):
    """已经吐过内容再切换会重复正文，只能直接上抛。"""
    calls: list[str] = []

    async def _fake(self, *args, **kwargs):
        calls.append(self.model_name)
        yield "chunk-1"
        raise RuntimeError("mid-stream failure")

    monkeypatch.setattr(ChatOpenAI, "_astream", _fake)
    model_registry.set_provider(await _provider(_candidate("main", MAIN)))
    await model_registry.refresh_candidates()

    got: list[str] = []
    with pytest.raises(RuntimeError, match="mid-stream failure"):
        async for c in _make_llm()._astream([]):
            got.append(c)
    assert got == ["chunk-1"]
    assert calls == [MAIN]  # 没有重放


# ── 客户端复用 ──────────────────────────────────────────────────────────

async def test_candidate_client_is_reused_across_calls():
    model_registry.set_provider(await _provider(_candidate("main", MAIN)))
    await model_registry.refresh_candidates()

    llm = _make_llm()
    first = llm._candidate_clients()[0]
    assert llm._candidate_clients()[0] is first  # 同一个候选只建一个客户端
