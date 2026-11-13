# 知识链路接线后的确定性单测。
#
# 回归点：
# - build_knowledge_tools 产出的工具必须真的能取到请求级依赖（contextvar）
# - 依赖缺失 / 基础设施不可用时返回可读提示，而不是抛异常打断整轮对话
# - NL2SQL 工具必须按角色拦截，患者端不能触达运营库
# - 轨迹采样开关要真的生效（TRACE_SAMPLE_RATE）

import asyncio

import pytest

from src.core import tracing
from src.knowledge import doc_rag
from src.knowledge.deps import KnowledgeDeps, bind_knowledge_deps, current_knowledge_deps
from src.knowledge.tools import build_knowledge_tools


def _deps(role: str = "patient", db_session=None) -> KnowledgeDeps:
    return KnowledgeDeps(
        llm=None,
        embedding_model=None,
        milvus_client=None,
        neo4j_driver=None,
        db_session=db_session,
        user_id="2",
        role=role,
    )


def _tools() -> dict:
    return {t.name: t for t in build_knowledge_tools()}


def test_bind_deps_sets_and_resets_contextvar():
    assert current_knowledge_deps() is None
    deps = _deps()
    with bind_knowledge_deps(deps):
        assert current_knowledge_deps() is deps
    assert current_knowledge_deps() is None


@pytest.mark.asyncio
async def test_docs_tool_without_deps_returns_hint():
    """未绑定依赖（例如单测/脚本直接调用）时也不能抛异常。"""
    out = await _tools()["search_knowledge_docs"].ainvoke({"question": "高血压饮食"})
    assert "依赖未初始化" in out


@pytest.mark.asyncio
async def test_docs_tool_degrades_when_milvus_missing():
    with bind_knowledge_deps(_deps()):
        out = await _tools()["search_knowledge_docs"].ainvoke({"question": "高血压饮食"})
    assert "暂时不可用" in out


@pytest.mark.asyncio
async def test_sql_tool_blocks_patient_role():
    with bind_knowledge_deps(_deps(role="patient", db_session=object())):
        out = await _tools()["search_knowledge_sql"].ainvoke({"question": "本月就诊量"})
    assert "无权访问" in out


@pytest.mark.asyncio
async def test_sql_tool_runs_for_staff_role(monkeypatch):
    async def fake_search_sql(question, llm, db):  # noqa: ARG001
        return "查询结果：本月就诊量 1200 人次"

    monkeypatch.setattr("src.knowledge.nl2sql.search_sql", fake_search_sql)

    with bind_knowledge_deps(_deps(role="staff", db_session=object())):
        out = await _tools()["search_knowledge_sql"].ainvoke({"question": "本月就诊量"})
    assert "1200" in out


@pytest.mark.asyncio
async def test_rewrite_falls_back_on_timeout(monkeypatch):
    async def slow_rewrite(*_args, **_kwargs):
        await asyncio.sleep(5)

    monkeypatch.setattr("src.knowledge.query_rewriter.rewrite_query", slow_rewrite)
    monkeypatch.setattr("src.knowledge.tools.REWRITE_TIMEOUT", 0.01)

    from src.knowledge.tools import _rewrite

    assert await _rewrite("高血压能吃咸的吗", _deps()) == "高血压能吃咸的吗"


@pytest.mark.asyncio
async def test_search_docs_returns_answer_when_grounding_times_out(monkeypatch):
    async def fake_raw(*_args, **_kwargs):
        return [{"doc_name": "指南.pdf", "page_number": 1, "text": "限盐"}]

    class _LLM:
        async def ainvoke(self, _messages):
            class _R:
                content = "每天食盐少于 5 克。"

            return _R()

    async def slow_check(**_kwargs):
        await asyncio.sleep(5)

    monkeypatch.setattr(doc_rag, "search_docs_raw", fake_raw)
    monkeypatch.setattr("src.knowledge.hallucination_check.check_hallucination", slow_check)
    monkeypatch.setattr(doc_rag, "GROUNDING_TIMEOUT", 0.01)

    answer = await doc_rag.search_docs(
        question="高血压限盐",
        embedding_model=None,
        milvus_client=None,
        llm=_LLM(),
        with_grounding=True,
    )
    assert answer == "每天食盐少于 5 克。"


def test_trace_sampling_skips_persist(monkeypatch):
    monkeypatch.setattr(tracing, "_should_sample", lambda: False)
    enqueued: list = []
    monkeypatch.setattr(tracing, "_enqueue_trace", lambda payload: enqueued.append(payload))

    recorder, token = tracing.start_trace(user_id="2", session_id="s1")
    assert recorder.sampled is False
    assert recorder.start_span("tool.x", "tool") is None

    recorder.count_tool("search_knowledge_docs")
    recorder.finish()
    tracing.end_trace(recorder, token)

    assert enqueued == []
