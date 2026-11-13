# 体征记录「风险解读」的确定性单测。
#
# 覆盖三件容易回归的事：
# - 问句必须带上数值/分级/测量状态，模型才能给出针对性解读
# - 知识库检索/模型任一环节失败都要降级成可读文本，不能给前端 5xx
# - 相关性门槛：不相关文档不能让解读"有依据"（库里曾混进简历 PDF）

from types import SimpleNamespace

import pytest

from src.modules.patient import metric_advice


def _metric(**overrides) -> metric_advice.MetricSnapshot:
    base = dict(
        metric_id=1,
        metric_type="blood_pressure",
        value_primary=152.0,
        value_secondary=96.0,
        unit="mmHg",
        level="high",
        period=None,
    )
    base.update(overrides)
    return metric_advice.MetricSnapshot(**base)


class _FakeLLM:
    def __init__(self, pieces=("【结论】偏高", "\n【可能的风险】", "\n- 风险一"), fail_at=None):
        self._pieces = pieces
        self._fail_at = fail_at

    async def astream(self, _messages):
        for idx, piece in enumerate(self._pieces):
            if self._fail_at is not None and idx == self._fail_at:
                raise RuntimeError("model down")
            yield SimpleNamespace(content=piece)


def test_build_question_carries_value_level_and_trend():
    question = metric_advice.build_question(
        _metric(
            gender="男",
            age=30,
            height_cm=175,
            weight_kg=70,
            recent_primary=[128.0, 152.0],
        )
    )

    assert "152/96 mmHg" in question
    assert "偏高" in question
    assert "18-39 岁" in question
    assert "近期 2 次测量均值 140" in question
    # 血压分级口径要和 evaluate_metric 一致，避免"平台说正常、模型说偏高"
    assert "140/90" in question


def test_fallback_text_has_four_sections():
    text = metric_advice.fallback_text(_metric(level="critical"))

    for section in ("【结论】", "【可能的风险】", "【值得注意】", "【何时就医】"):
        assert section in text
    assert "尽快就医" in text


@pytest.mark.asyncio
async def test_stream_advice_emits_meta_sources_then_tokens(monkeypatch):
    async def _fake_retrieve(_question):
        return [{"doc_name": "高血压指南.pdf", "page_number": 3, "text": "限盐"}]

    monkeypatch.setattr(metric_advice, "retrieve_docs", _fake_retrieve)
    monkeypatch.setattr(metric_advice, "get_llm", lambda **_kw: _FakeLLM())

    events = [e async for e in metric_advice.stream_advice(_metric())]

    assert events[0]["type"] == "meta"
    assert events[0]["value_text"] == "152/96 mmHg"
    assert events[1]["type"] == "sources"
    assert events[1]["grounded"] is True
    assert events[1]["sources"][0]["doc_name"] == "高血压指南.pdf"
    assert events[-1] == {"type": "done", "grounded": True}
    assert "".join(e["content"] for e in events if e["type"] == "token") == (
        "【结论】偏高\n【可能的风险】\n- 风险一"
    )


@pytest.mark.asyncio
async def test_stream_advice_degrades_when_model_fails(monkeypatch):
    async def _fake_retrieve(_question):
        return []

    monkeypatch.setattr(metric_advice, "retrieve_docs", _fake_retrieve)
    monkeypatch.setattr(metric_advice, "get_llm", lambda **_kw: _FakeLLM(fail_at=0))

    events = [e async for e in metric_advice.stream_advice(_metric())]

    text = "".join(e["content"] for e in events if e["type"] == "token")
    assert "【结论】" in text and "【何时就医】" in text
    assert any(e["type"] == "notice" for e in events)
    assert events[-1]["type"] == "done"


def test_is_relevant_uses_rerank_then_vector_score():
    assert metric_advice._is_relevant({"rerank_score": 0.8})
    assert not metric_advice._is_relevant({"rerank_score": 0.1})
    assert metric_advice._is_relevant({"score": 0.6})
    # 向量库里混进的不相关文档（如简历）相似度很低
    assert not metric_advice._is_relevant({"score": 0.26})
    # 没有分数信息时不做判断，交给模型自行取舍
    assert metric_advice._is_relevant({})
