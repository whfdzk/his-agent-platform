# 配置与模型客户端工厂的确定性单测。
# 重点：API Key 优先级必须只有一种口径，否则同一份 .env 在不同代码路径会选出不同厂商。

from types import SimpleNamespace

import pytest

from src.core import config
from src.core.exceptions import BizException


def _settings(**overrides):
    base = dict(
        DEEPSEEK_API_KEY="", OPENAI_API_KEY="", DASHSCOPE_API_KEY="",
        BASE_URL_CHAT="", CHAT_MODEL="test-model",
        LLM_TIMEOUT=1.0, LLM_MAX_RETRIES=0,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture(autouse=True)
def _clear_client_caches():
    config.get_llm.cache_clear()
    config.get_async_openai.cache_clear()
    yield
    config.get_llm.cache_clear()
    config.get_async_openai.cache_clear()


def test_chat_credentials_prefer_deepseek(monkeypatch):
    monkeypatch.setattr(config, "get_settings", lambda: _settings(
        DEEPSEEK_API_KEY="ds", OPENAI_API_KEY="oa", DASHSCOPE_API_KEY="dash",
        BASE_URL_CHAT="http://gateway/v1",
    ))
    assert config._resolve_chat_credentials() == ("ds", "http://gateway/v1")


def test_chat_credentials_fall_back_to_openai(monkeypatch):
    monkeypatch.setattr(config, "get_settings", lambda: _settings(
        OPENAI_API_KEY="oa", DASHSCOPE_API_KEY="dash", BASE_URL_CHAT="http://gateway/v1",
    ))
    assert config._resolve_chat_credentials() == ("oa", "http://gateway/v1")


def test_chat_credentials_strip_chat_completions_suffix(monkeypatch):
    monkeypatch.setattr(config, "get_settings", lambda: _settings(
        DEEPSEEK_API_KEY="ds", BASE_URL_CHAT="http://gateway/v1/chat/completions",
    ))
    assert config._resolve_chat_credentials() == ("ds", "http://gateway/v1")


def test_chat_credentials_reject_missing_keys(monkeypatch):
    monkeypatch.setattr(config, "get_settings", lambda: _settings())
    with pytest.raises(BizException):
        config._resolve_chat_credentials()


def test_get_llm_reuses_client_for_same_arguments(monkeypatch):
    monkeypatch.setattr(config, "_resolve_chat_credentials", lambda: ("key", None))
    monkeypatch.setattr(config, "get_settings", lambda: _settings())

    assert config.get_llm(temperature=0.3, model="m") is config.get_llm(temperature=0.3, model="m")


def test_get_llm_keeps_clients_apart_for_different_models(monkeypatch):
    monkeypatch.setattr(config, "_resolve_chat_credentials", lambda: ("key", None))
    monkeypatch.setattr(config, "get_settings", lambda: _settings())

    assert config.get_llm(0.3, model="a") is not config.get_llm(0.3, model="b")


def test_get_async_openai_is_shared(monkeypatch):
    monkeypatch.setattr(config, "_resolve_chat_credentials", lambda: ("key", None))

    assert config.get_async_openai() is config.get_async_openai()


def test_timeout_budget_is_coherent():
    """超时预算必须自洽，否则正常（但慢）的一轮会被整轮超时误杀。

    - TURN_TIMEOUT 要能装下单次 LLM 调用的最坏耗时（含重试）；
    - WORKER_TIMEOUT 要明显小于 TURN_TIMEOUT，子图跑满后 supervisor 还得有收尾时间。
    """
    s = config.get_settings()
    worst_single_llm_call = s.LLM_TIMEOUT * (s.LLM_MAX_RETRIES + 1)
    assert s.TURN_TIMEOUT > worst_single_llm_call
    assert s.WORKER_TIMEOUT < s.TURN_TIMEOUT


def test_retrieval_settings_have_sane_defaults():
    settings = config.get_settings()
    assert settings.RETRIEVAL_TOP_K >= settings.RERANK_TOP_K
    assert settings.NL2SQL_ROW_LIMIT > 0
    assert settings.NL2SQL_TIMEOUT_SECONDS > 0
    assert settings.EMBEDDING_BASE_URL
