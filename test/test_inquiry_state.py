# 问诊状态的确定性单测。
# InquiryState 经 JSON 序列化进 Redis，是跨轮次恢复的唯一载体，
# 字段丢失会直接导致问诊流程错乱。

from pydantic import ValidationError
import pytest

from src.agents.inquiry.state import (
    CandidateDisease,
    InquiryHandoffPayload,
    InquiryPhase,
    InquiryState,
)


def test_default_state_starts_at_clarify():
    state = InquiryState()
    assert state.phase == InquiryPhase.CLARIFY
    assert state.round == 0
    assert state.confirmed_symptoms == []


def test_end_phase_matches_langgraph_end_sentinel():
    assert InquiryPhase.END.value == "__end__"


def test_state_survives_redis_round_trip():
    state = InquiryState(
        phase=InquiryPhase.SYMPTOM_CONFIRM,
        round=3,
        session_id="s1",
        confirmed_symptoms=["发热", "咳嗽"],
        denied_symptoms=["胸痛"],
        asked_symptoms=["发热"],
        candidate_diseases=[
            CandidateDisease(name="上呼吸道感染", confidence=0.8, department="呼吸内科"),
        ],
    )
    restored = InquiryState.model_validate_json(state.model_dump_json())

    assert restored.phase == InquiryPhase.SYMPTOM_CONFIRM
    assert restored.round == 3
    assert restored.session_id == "s1"
    assert restored.confirmed_symptoms == ["发热", "咳嗽"]
    assert restored.denied_symptoms == ["胸痛"]
    assert restored.asked_symptoms == ["发热"]
    assert restored.candidate_diseases[0].name == "上呼吸道感染"
    assert restored.candidate_diseases[0].confidence == 0.8


def test_handoff_payload_round_trip():
    payload = InquiryHandoffPayload(
        primary_disease="急性上呼吸道感染",
        primary_confidence=0.75,
        confirmed_symptoms=["发热"],
        department="呼吸内科",
        total_rounds=4,
    )
    state = InquiryState(phase=InquiryPhase.HANDOFF, handoff_payload=payload)
    restored = InquiryState.model_validate_json(state.model_dump_json())

    assert restored.handoff_payload is not None
    assert restored.handoff_payload.primary_disease == "急性上呼吸道感染"
    assert restored.handoff_payload.department == "呼吸内科"
    assert restored.handoff_payload.total_rounds == 4


def test_unknown_phase_is_rejected():
    with pytest.raises(ValidationError):
        InquiryState(phase="NOT_A_PHASE")
