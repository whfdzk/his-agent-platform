# 基础设施客户端的确定性单测。
# 这些客户端持有 gRPC / HTTP 连接，每请求新建会导致连接泄漏，必须保证是单例。

from types import SimpleNamespace

import pytest

from src.core.exceptions import BizException
from src.infra import embedding as embedding_module
from src.infra import milvus_client as milvus_module


class _FakeMilvusClient:
    def __init__(self, uri: str) -> None:
        self.uri = uri
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _embedding_settings(**overrides):
    base = dict(EMBEDDING_API_KEY="", EMBEDDING_MODEL="m", EMBEDDING_BASE_URL="")
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture(autouse=True)
def _clear_singletons():
    embedding_module.get_embeddings.cache_clear()
    previous = milvus_module._shared_client
    milvus_module._shared_client = None
    yield
    embedding_module.get_embeddings.cache_clear()
    milvus_module._shared_client = previous


def test_get_embeddings_returns_shared_instance(monkeypatch):
    monkeypatch.setattr(embedding_module, "get_settings",
                        lambda: _embedding_settings(EMBEDDING_API_KEY="k"))

    assert embedding_module.get_embeddings() is embedding_module.get_embeddings()


def test_get_embeddings_requires_api_key(monkeypatch):
    monkeypatch.setattr(embedding_module, "get_settings", lambda: _embedding_settings())

    with pytest.raises(BizException):
        embedding_module.get_embeddings()


def test_get_milvus_client_returns_shared_instance(monkeypatch):
    created: list[_FakeMilvusClient] = []

    def _factory(uri: str) -> _FakeMilvusClient:
        client = _FakeMilvusClient(uri)
        created.append(client)
        return client

    monkeypatch.setattr(milvus_module, "MilvusClient", _factory)
    monkeypatch.setattr(milvus_module, "settings",
                        SimpleNamespace(MILVUS_HOST="milvus", MILVUS_PORT="19530"))

    first = milvus_module.get_milvus_client()
    second = milvus_module.get_milvus_client()

    assert first is second
    assert len(created) == 1  # 只建过一次连接
    assert first.uri == "http://milvus:19530"


def test_close_milvus_client_releases_shared_instance(monkeypatch):
    monkeypatch.setattr(milvus_module, "MilvusClient", _FakeMilvusClient)
    monkeypatch.setattr(milvus_module, "settings",
                        SimpleNamespace(MILVUS_HOST="milvus", MILVUS_PORT="19530"))
    monkeypatch.setattr(milvus_module, "connections", SimpleNamespace(disconnect=lambda alias: None))

    client = milvus_module.get_milvus_client()
    milvus_module.close_milvus_client()

    assert client.closed
    assert milvus_module._shared_client is None
