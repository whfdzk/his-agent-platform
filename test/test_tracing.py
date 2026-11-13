"""全链路追踪：字段完整性 + 脱敏 + 落库投递。"""

import pytest

from src.core import tracing
from src.core.redaction import contains_phi, redact_payload, redact_text


@pytest.fixture
def captured(monkeypatch):
    items = []

    def _fake_enqueue(item):
        items.append(item)

    monkeypatch.setattr(tracing, "enqueue", _fake_enqueue)
    return items


def test_redact_text_masks_phi():
    text = "张三 13800138000 身份证 110101199001011234 报告原文"
    masked = redact_text(text)
    assert "13800138000" not in masked
    assert "110101199001011234" not in masked
    assert not contains_phi(masked)


def test_redact_payload_drops_sensitive_keys():
    payload = redact_payload({"content": "病人主诉胸痛", "phone": "13800138000", "count": 3})
    assert payload["content"] == "<redacted>"
    assert payload["phone"] == "<redacted>"
    assert payload["count"] == 3


def test_trace_records_full_chain(captured):
    recorder, token = tracing.start_trace(
        user_id="1001", session_id="s1", turn_id="t1",
        entrypoint="/api/v1/chat/stream", agent_name="supervisor",
    )
    recorder.input_chars = 12
    recorder.count_agent("supervisor")
    recorder.count_tool("call_knowledge_agent")
    recorder.add_tokens(120, 80, model_name="deepseek-chat", model_version="fp-1")
    recorder.add_retrieval("milvus", [{"doc_id": "chunk-1", "doc_name": "指南", "score": 0.82}])
    recorder.set_safety("high", "warn", ["em_bp_high"])

    span = recorder.start_span("llm.call", "llm", model_name="deepseek-chat")
    recorder.end_span(span, status="ok", prompt_tokens=120, completion_tokens=80)

    tracing.end_trace(recorder, token, status="success")

    traces = [i.payload for i in captured if i.kind == "agent_trace"]
    spans = [i.payload for i in captured if i.kind == "agent_span"]
    assert len(traces) == 1
    trace = traces[0]
    assert trace["trace_id"] == recorder.trace_id
    assert trace["agent_version"] and trace["prompt_version"]
    assert trace["model_name"] == "deepseek-chat"
    assert trace["model_version"] == "fp-1"
    assert trace["total_tokens"] == 200
    assert trace["tool_calls"] == 1
    assert trace["retrieval_count"] == 1
    assert trace["safety_result"] == "warn"
    assert trace["status"] == "success"

    llm_spans = [s for s in spans if s["kind"] == "llm"]
    assert llm_spans and llm_spans[0]["total_tokens"] == 200
    assert llm_spans[0]["trace_id"] == recorder.trace_id
    assert any(s["kind"] == "retrieval" for s in spans)


def test_trace_attributes_are_redacted(captured):
    recorder, token = tracing.start_trace(user_id="1001", session_id="s1")
    span = recorder.start_span(
        "tool.call", "tool",
        attributes={"content": "患者身份证 110101199001011234", "tool_name": "x"},
    )
    recorder.end_span(span)
    tracing.end_trace(recorder, token)

    span_payload = [i.payload for i in captured if i.kind == "agent_span"][0]
    dumped = str(span_payload)
    assert "110101199001011234" not in dumped


def test_trace_finish_is_idempotent(captured):
    recorder, token = tracing.start_trace(user_id="1", session_id="s")
    tracing.end_trace(recorder, token)
    tracing.end_trace(recorder, None)
    assert len([i for i in captured if i.kind == "agent_trace"]) == 1
