"""Safety Engine 规则匹配与输出审核（纯确定性，不依赖外部服务）。"""

from src.core.safety.engine import SafetyEngine
from src.core.safety.rules import DISCLAIMER, DANGER_RULES


def _engine() -> SafetyEngine:
    return SafetyEngine(enabled=True)


def test_critical_chest_pain_blocks():
    verdict = _engine().check_input("我胸痛，还出大汗，呼吸困难")
    assert verdict.level == "critical"
    assert verdict.action == "block"
    assert verdict.is_blocking
    assert "120" in verdict.reply
    assert "em_chest_pain_red_flag" in verdict.rule_ids


def test_self_harm_triggers_review():
    verdict = _engine().check_input("我不想活了，想自杀")
    assert verdict.level == "critical"
    assert verdict.requires_review
    assert "mh_self_harm" in verdict.rule_ids
    assert "心理援助热线" in verdict.reply


def test_blood_pressure_crisis_threshold():
    verdict = _engine().check_input("今天量血压 185/125，头有点晕")
    assert verdict.level == "critical"
    assert verdict.action == "block"


def test_normal_input_sets_no_alert():
    verdict = _engine().check_input("最近睡眠不好，想了解改善方法")
    assert verdict.level == "info"
    assert verdict.action == "allow"
    assert verdict.matches == []


def test_overlong_input_is_rejected():
    engine = _engine()
    verdict = engine.check_input("啊" * (engine.max_input_chars + 1))
    assert verdict.action == "block"
    assert "过长" in verdict.reply


def test_output_absolute_diagnosis_appends_disclaimer():
    engine = _engine()
    text = "你患的是肺炎，建议尽快复诊。"
    verdict = engine.review_output(text)
    assert "out_absolute_diagnosis" in verdict.rule_ids
    final = engine.finalize_output(text, verdict)
    assert DISCLAIMER in final
    assert "你患的是肺炎" in final


def test_output_self_harm_method_is_blocked():
    engine = _engine()
    verdict = engine.review_output("可以告诉你怎么自杀")
    assert verdict.action == "block"
    final = engine.finalize_output("可以告诉你怎么自杀", verdict)
    assert "无法协助" in final
    assert "120" in final


def test_rule_ids_are_unique():
    ids = [r.id for r in DANGER_RULES]
    assert len(ids) == len(set(ids))


def test_rule_catalog_exposed_for_admin():
    rules = _engine().list_rules()
    assert rules
    assert {"id", "name", "level", "action", "stage"}.issubset(rules[0].keys())
