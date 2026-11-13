# src/core/safety/engine.py
#
# Safety Engine：输入危险信号识别 + 输出合规审核。
#
# 调用位点（见 api/routers/chat.py）：
#   1. 用户消息进入 Agent 之前 → check_input
#      critical 命中直接给出急救指引并阻断本轮（不再调用 LLM，避免"AI 建议再观察"）
#   2. Agent 输出推送给用户之前 → review_output
#      绝对化诊断、具体剂量、疗效保证等追加免责声明；自伤方法类直接替换
#
# 每次审核都写安全事件（safety_events）并挂到 trace 的 safety span 上。

from __future__ import annotations

from dataclasses import dataclass, field

from src.core.config import get_settings
from src.core.safety.rules import (
    DISCLAIMER,
    EMERGENCY_DISCLAIMER,
    DangerRule,
    rules_for,
)

_LEVEL_ORDER = {"info": 0, "warn": 1, "high": 2, "critical": 3}
_ACTION_ORDER = {"allow": 0, "warn": 1, "escalate": 2, "block": 3}


@dataclass
class SafetyVerdict:
    """一次安全审核的结论。"""

    level: str = "info"
    action: str = "allow"
    matches: list[DangerRule] = field(default_factory=list)
    reply: str = ""
    requires_review: bool = False
    stage: str = "input"

    @property
    def rule_ids(self) -> list[str]:
        return [r.id for r in self.matches]

    @property
    def rule_names(self) -> list[str]:
        return [r.name for r in self.matches]

    @property
    def is_blocking(self) -> bool:
        return self.action == "block"

    @property
    def primary(self) -> DangerRule | None:
        return self.matches[0] if self.matches else None

    def summary(self) -> str:
        if not self.matches:
            return "未命中危险信号"
        return "；".join(f"{r.name}({r.level})" for r in self.matches)

    def to_dict(self) -> dict:
        return {
            "level": self.level,
            "action": self.action,
            "rules": self.rule_ids,
            "rule_names": self.rule_names,
            "stage": self.stage,
            "requires_review": self.requires_review,
        }


class SafetyEngine:
    """规则驱动的安全审核器（确定性、可测试、可审计）。"""

    def __init__(self, enabled: bool | None = None) -> None:
        settings = get_settings()
        self.enabled = settings.SAFETY_ENABLED if enabled is None else enabled
        self.max_input_chars = settings.SAFETY_MAX_INPUT_CHARS
        self.block_on_critical = settings.SAFETY_BLOCK_ON_CRITICAL
        self.review_level = settings.SAFETY_REVIEW_REQUIRED_LEVEL
        self._input_rules = rules_for("input")
        self._output_rules = rules_for("output")

    # ── 输入审核 ────────────────────────────────────────────────────────
    def check_input(self, text: str) -> SafetyVerdict:
        if not self.enabled or not text:
            return SafetyVerdict(stage="input")
        if len(text) > self.max_input_chars:
            return SafetyVerdict(
                level="warn",
                action="block",
                reply=f"输入内容过长（上限 {self.max_input_chars} 字），请精简后重试。",
                stage="input",
            )
        verdict = self._evaluate(text, self._input_rules, stage="input")
        if verdict.is_blocking and not self.block_on_critical:
            verdict.action = "warn"
        if verdict.primary is not None:
            verdict.reply = verdict.primary.advice
            if verdict.level == "critical":
                verdict.reply = f"{verdict.primary.advice}\n\n{EMERGENCY_DISCLAIMER}"
        return verdict

    # ── 输出审核 ────────────────────────────────────────────────────────
    def review_output(self, text: str, *, emergency: bool = False) -> SafetyVerdict:
        if not self.enabled or not text:
            return SafetyVerdict(stage="output")
        verdict = self._evaluate(text, self._output_rules, stage="output")
        if emergency:
            verdict.level = _max_level(verdict.level, "high")
            verdict.requires_review = True
        return verdict

    def finalize_output(self, text: str, verdict: SafetyVerdict | None = None) -> str:
        """按输出审核结论加工最终回复：阻断替换、提示追加免责声明。"""
        body = (text or "").strip()
        if verdict is None or verdict.action == "allow":
            return self._append_disclaimer(body)
        if verdict.action == "block":
            return (
                "抱歉，这个请求我无法协助。如果你或身边的人正处于危险中，"
                "请立即拨打 120 或心理援助热线 400-161-9995。"
            )
        # warn / escalate：保留原回复，但补齐免责声明与具体规则提示
        clean = _strip_existing_disclaimer(body)
        rule_hint = verdict.primary.advice if verdict.primary else ""
        tail = f"{rule_hint}\n{DISCLAIMER}" if rule_hint else DISCLAIMER
        return f"{clean}\n\n> {tail}" if clean else f"> {tail}"

    def _append_disclaimer(self, text: str) -> str:
        if not text:
            return text
        if DISCLAIMER in text:
            return text
        return f"{text}\n\n> {DISCLAIMER}"

    # ── 内部 ────────────────────────────────────────────────────────────
    def _evaluate(self, text: str, rules: tuple[DangerRule, ...], *, stage: str) -> SafetyVerdict:
        matched: list[DangerRule] = []
        for rule in rules:
            try:
                if rule.matches(text):
                    matched.append(rule)
            except Exception:
                # 单条规则异常不能影响整体审核
                continue
        if not matched:
            return SafetyVerdict(stage=stage)

        # critical 优先展示，其次 high
        matched.sort(key=lambda r: (-_LEVEL_ORDER.get(r.level, 0), r.id))
        level = matched[0].level
        action = "allow"
        for r in matched:
            if _ACTION_ORDER.get(r.action, 0) > _ACTION_ORDER.get(action, 0):
                action = r.action
        requires_review = any(r.requires_review for r in matched) or (
            _LEVEL_ORDER.get(level, 0) >= _LEVEL_ORDER.get(self.review_level, 2)
        )
        if action == "block":
            # 阻断一定同时进入人工复核队列
            requires_review = True
        return SafetyVerdict(
            level=level,
            action=action,
            matches=matched,
            requires_review=requires_review,
            stage=stage,
        )

    # ── 规则维护 ────────────────────────────────────────────────────────
    def list_rules(self) -> list[dict]:
        return [
            {
                "id": r.id,
                "name": r.name,
                "category": r.category,
                "level": r.level,
                "action": r.action,
                "stage": r.stage,
                "requires_review": r.requires_review,
            }
            for r in (*self._input_rules, *self._output_rules)
        ]


def _max_level(a: str, b: str) -> str:
    return a if _LEVEL_ORDER.get(a, 0) >= _LEVEL_ORDER.get(b, 0) else b


def _strip_existing_disclaimer(text: str) -> str:
    return text.replace(f"\n\n> {DISCLAIMER}", "").replace(DISCLAIMER, "").strip()


safety_engine = SafetyEngine()
