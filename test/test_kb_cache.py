# test/test_kb_cache.py
#
# 检索结果缓存：命中 / 并发合并 / 过期。

import asyncio

from src.core.config import get_llm
from src.knowledge.cache import cached, clear, make_key, stats
from src.knowledge.deps import KnowledgeDeps


def test_cache_hits_without_recomputing():
    clear()
    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        return ["hit"]

    async def run():
        first = await cached("k1", factory, ttl=60)
        second = await cached("k1", factory, ttl=60)
        return first, second

    a, b = asyncio.run(run())
    assert a == b == ["hit"]
    assert calls["n"] == 1
    assert stats()["hit"] == 1


def test_cache_merges_concurrent_misses():
    clear()
    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        await asyncio.sleep(0.05)
        return "value"

    async def run():
        return await asyncio.gather(
            cached("k2", factory, ttl=60), cached("k2", factory, ttl=60),
        )

    results = asyncio.run(run())
    assert results == ["value", "value"]
    assert calls["n"] == 1


def test_cache_expires():
    clear()
    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        return calls["n"]

    async def run():
        return [await cached("k3", factory, ttl=0) for _ in range(3)]

    assert asyncio.run(run()) == [1, 2, 3]


def test_keys_distinguish_params():
    assert make_key("docs", "高血压", None) != make_key("docs", "高血压", "guideline")
    assert make_key("docs", "高血压", None) == make_key("docs", "高血压", None)


def test_util_llm_disables_thinking_only_for_qwen():
    util = get_llm(temperature=0.0, thinking=False, max_tokens=512)
    assert util.extra_body == {"enable_thinking": False}
    assert util.max_tokens == 512

    normal = get_llm(temperature=0.7)
    assert normal.extra_body is None


def test_knowledge_deps_staff_alignment():
    llm = get_llm(temperature=0.0)
    deps = KnowledgeDeps(
        llm=llm, util_llm=llm, embedding_model=None,
        milvus_client=None, neo4j_driver=None, role="clinician",
    )
    assert deps.is_staff is True
    assert deps.util_llm is llm
    patient = KnowledgeDeps(
        llm=llm, embedding_model=None, milvus_client=None,
        neo4j_driver=None, role="patient",
    )
    assert patient.is_staff is False
    assert patient.util_llm is llm  # 未显式给 util_llm 时退回主模型
