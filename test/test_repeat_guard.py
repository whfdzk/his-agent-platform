"""打转判定与角色门禁的单测（纯同步，不依赖 LLM 与 Redis）。"""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage

from src.agents.repeat_middleware import is_repeat_call
from src.core.permissions import is_staff_role
from src.core.safety.repeat import find_repeats, fingerprint


def _call(name: str, args: dict, call_id: str) -> dict:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


def test_fingerprint_ignores_key_order():
    a = fingerprint("search_kb", {"question": "头痛", "top_k": 5})
    b = fingerprint("search_kb", {"top_k": 5, "question": "头痛"})
    assert a == b


def test_fingerprint_differs_on_args_and_name():
    assert fingerprint("t", {"q": "甲"}) != fingerprint("t", {"q": "乙"})
    assert fingerprint("t1", {"q": "甲"}) != fingerprint("t2", {"q": "甲"})


def test_find_repeats_flags_second_identical_call():
    calls = [
        _call("search_disease", {"symptom": "头痛"}, "1"),
        _call("search_disease", {"symptom": "头痛"}, "2"),
    ]
    assert find_repeats(calls) == [1]


def test_find_repeats_ignores_different_args():
    """连查三个不同症状是有效进展，不能被判打转。"""
    calls = [
        _call("search_disease", {"symptom": "头痛"}, "1"),
        _call("search_disease", {"symptom": "腹痛"}, "2"),
        _call("search_disease", {"symptom": "胸闷"}, "3"),
    ]
    assert find_repeats(calls) == []


def test_find_repeats_respects_window():
    calls = [
        _call("search_kb", {"q": "x"}, "1"),
        _call("other_a", {}, "2"),
        _call("other_b", {}, "3"),
        _call("other_c", {}, "4"),
        _call("search_kb", {"q": "x"}, "5"),
    ]
    assert find_repeats(calls, window=3) == []
    assert find_repeats(calls, window=4) == [4]


def test_find_repeats_skips_exempt_tools():
    calls = [
        _call("search_memory", {"q": "过敏史"}, "1"),
        _call("search_memory", {"q": "过敏史"}, "2"),
    ]
    assert find_repeats(calls) == []


def test_is_repeat_call_sees_previous_ai_message():
    history = AIMessage(content="", tool_calls=[_call("search_kb", {"q": "头痛"}, "c1")])
    messages = [
        HumanMessage(content="我头痛"),
        history,
        AIMessage(content="", tool_calls=[_call("search_kb", {"q": "头痛"}, "c2")]),
    ]
    assert is_repeat_call(messages, _call("search_kb", {"q": "头痛"}, "c2")) is True
    assert is_repeat_call(messages, _call("search_kb", {"q": "腹痛"}, "c3")) is False


def test_is_repeat_call_counts_parallel_duplicates():
    """同一条 AIMessage 里并发发两个相同调用，第二个要算重复。"""
    ai = AIMessage(content="", tool_calls=[
        _call("search_kb", {"q": "同一个问题"}, "p1"),
        _call("search_kb", {"q": "同一个问题"}, "p2"),
    ])
    assert is_repeat_call([HumanMessage(content="hi"), ai], ai.tool_calls[1]) is True
    assert is_repeat_call([HumanMessage(content="hi"), ai], ai.tool_calls[0]) is False


def test_is_staff_role():
    assert is_staff_role("clinician") is True
    assert is_staff_role("admin") is True
    assert is_staff_role("patient") is False
    assert is_staff_role("") is False
    assert is_staff_role(None) is False
