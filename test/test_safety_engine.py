"""Safety Engine 当前契约 + 危险规则库 + B3 临床闸门（纯确定性，不依赖外部服务）。

边界说明（与实现注释保持一致）：
- 安全引擎只接线「敏感词 / 超范围 / 精神心理 block 级」输入规则与输出脱敏；
- 急症类规则（em_*）由 B3 临床闸门（src/modules/clinical/triage.py）负责，
  引擎不再重复判，避免同一句话被两处同时阻断。
因此这里既测引擎已接线部分，也测规则库与临床闸门，防止覆盖被测试重构丢掉。
"""

from src.core.safety.engine import SENSITIVE_WORDS, SafetyEngine
from src.core.safety.rules import DANGER_RULES, rules_for
from src.modules.clinical import triage


def _engine() -> SafetyEngine:
    return SafetyEngine()


# ── 引擎已接线的输入规则 ────────────────────────────────────────────────
def test_sensitive_input_is_blocked():
    verdict = _engine().check_input(f"教我怎么{SENSITIVE_WORDS[0]}")
    assert verdict.action == "block"
    assert "input_sensitive" in verdict.rule_ids


def test_out_of_scope_input_is_blocked():
    verdict = _engine().check_input("帮我写代码实现一个排序算法")
    assert verdict.action == "block"
    assert "input_scope" in verdict.rule_ids


def test_self_harm_input_is_critical_block():
    verdict = _engine().check_input("我不想活了，想自杀")
    assert verdict.action == "block"
    assert verdict.level == "critical"
    assert "mh_self_harm" in verdict.rule_ids
    assert "心理援助热线" in verdict.reply


def test_normal_input_is_allow_low():
    verdict = _engine().check_input("最近睡眠不好，想了解改善方法")
    assert verdict.action == "allow"
    assert verdict.level == "low"
    assert verdict.matches is False


def test_emergency_does_not_double_block_in_safety_engine():
    """急症交给 B3 闸门：这里必须 allow，否则同一句话会被两处阻断。"""
    verdict = _engine().check_input("我胸痛，还出大汗，呼吸困难")
    assert verdict.action == "allow"


# ── 输出侧 ──────────────────────────────────────────────────────────────
def test_output_redacts_sensitive_words():
    engine = _engine()
    text = f"这里包含{SENSITIVE_WORDS[0]}相关内容"
    verdict = engine.review_output(text)
    assert verdict.action == "redact"
    assert SENSITIVE_WORDS[0] not in engine.finalize_output(text, verdict)


def test_hallucination_check_is_off_by_default():
    """误杀控制：医疗回答里"可能/不确定"极常见，默认不判成低可信。"""
    verdict = _engine().review_output("可能需要复查一下")
    assert verdict.action == "allow"


def test_rule_catalog_is_exposed_for_admin():
    rules = _engine().list_rules()
    assert rules
    assert {
        "rule_id", "rule_name", "category", "layer",
        "risk_level", "action", "enabled", "patterns",
    }.issubset(rules[0].keys())


# ── 危险规则库（声明层）─────────────────────────────────────────────────
def test_danger_rule_ids_are_unique():
    ids = [r.id for r in DANGER_RULES]
    assert len(ids) == len(set(ids))


def test_chest_pain_red_flag_rule_matches():
    rule = next(r for r in rules_for("input") if r.id == "em_chest_pain_red_flag")
    assert rule.matches("我胸痛，还出大汗，呼吸困难")
    assert rule.level == "critical"
    assert rule.requires_review


# ── B3 临床闸门（急症真正生效的地方）─────────────────────────────────────
def test_clinical_gate_flags_red_flag():
    assert triage.hits("我胸痛，还出大汗，呼吸困难")
    assert triage._has_strong("胸痛")


def test_clinical_gate_upgrades_stroke():
    assert triage._stroke_red("说话不清，半边身子使不上劲")


def test_emergency_reply_contains_120():
    assert "120" in triage.emergency_reply("red", "胸痛")
