"""入口路由规则单测。

只测纯规则分支（同步、无模型调用）：规则判定错会让单域问题错误地进 supervisor，
把 150 秒 500 的问题原样带回来，所以每条关键词都要锁住。
"""

from src.agents.router import classify_by_rules, has_drug_name


def test_drug_question_routes_to_drug():
    d = classify_by_rules("阿司匹林有什么常见副作用")
    assert d is not None
    assert d.mode == "single"
    assert d.agent == "drug"


def test_knowledge_question_routes_to_knowledge():
    d = classify_by_rules("糖尿病早期症状有哪些")
    assert d is not None
    assert (d.mode, d.agent) == ("single", "knowledge")


def test_drug_name_without_keyword_still_drug():
    d = classify_by_rules("布洛芬怎么吃")
    assert d is not None and d.agent == "drug"


def test_two_independent_requests_go_multi():
    d = classify_by_rules("阿司匹林副作用，另外我该挂什么科")
    assert d is not None
    assert d.mode == "multi"


def test_explicit_actions_never_bypass_supervisor():
    for q in ("帮我看看这个血常规报告", "帮我记一下明天复诊", "我该挂什么科"):
        d = classify_by_rules(q)
        assert d is None or d.mode == "multi", q


def test_unknown_text_defers_to_llm():
    assert classify_by_rules("高血压平时要注意什么") is None


def test_has_drug_name():
    assert has_drug_name("阿莫西林和布洛芬能一起吃吗")
    assert not has_drug_name("高血压平时要注意什么")
