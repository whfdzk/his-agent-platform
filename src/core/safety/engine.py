# src/core/safety/engine.py
#
# 安全引擎统一入口：所有请求都必须经过这层防护。
#
# 设计：规则表驱动 —— 词表、风险等级、命中动作集中声明在 RiskRule 里，
# 审核逻辑按 layer 依次匹配，产出 SafetyVerdict（含命中的 rule_ids 与建议话术）。
# 管理端规则库展示（/safety/rules）与审核判定共用同一份规则对象，避免两处漂移。

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from loguru import logger

# 规则词表：单一事实来源，审核逻辑与管理端规则库展示共用。
SENSITIVE_WORDS: list[str] = ["赌博", "色情", "暴力"]
OUT_OF_SCOPE_PATTERNS: list[str] = ["写代码", "做数学题", "翻译文章"]
HALLUCINATION_MARKERS: list[str] = ["不确定", "可能", "大概"]

REDACT_MASK = "***"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class SafetyAction(str, Enum):
    ALLOW = "allow"
    WARN = "warn"
    BLOCK = "block"
    REDACT = "redact"


# 动作严重度：多条规则同时命中时取最重的那条作为主规则
_ACTION_RANK = {
    SafetyAction.ALLOW: 0,
    SafetyAction.WARN: 1,
    SafetyAction.REDACT: 2,
    SafetyAction.BLOCK: 3,
}


@dataclass(frozen=True)
class RiskRule:
    """一条危险信号规则。"""

    rule_id: str
    rule_name: str
    category: str
    # 作用层：input（用户输入）/ output（模型输出）/ tool（工具调用行为）
    layer: str
    action: SafetyAction
    risk_level: RiskLevel
    patterns: list[str]
    advice: str = ""
    # 命中阻断时给用户的兜底话术；留空则由调用方使用自己的默认文案
    reply: str = ""

    def match(self, text: str) -> str | None:
        """返回命中的第一个词，未命中返回 None。"""
        for p in self.patterns:
            if p and p in text:
                return p
        return None

    def as_dict(self, enabled: bool) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "rule_name": self.rule_name,
            "category": self.category,
            "layer": self.layer,
            "risk_level": self.risk_level.value,
            "action": self.action.value,
            "enabled": enabled,
            "patterns": list(self.patterns),
        }


@dataclass
class SafetyVerdict:
    """一次审核的结论。字段与 API 层（chat.py）的消费方式对齐。"""

    action: str
    level: str
    matches: bool = False
    rule_ids: list[str] = field(default_factory=list)
    # 命中最重的规则，API 层用它取 advice（安全提示文案）
    primary: RiskRule | None = None
    reply: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def is_blocking(self) -> bool:
        return self.action == SafetyAction.BLOCK.value


@dataclass
class SafetyEvent:
    event_type: str
    user_id: str
    risk_level: RiskLevel
    action: SafetyAction
    reason: str
    timestamp: datetime = field(default_factory=datetime.now)


# 兼容旧调用方的结果结构（当前 chat 链路已改用 SafetyVerdict）
@dataclass
class SafetyResult:
    action: SafetyAction
    risk_level: RiskLevel = RiskLevel.LOW
    reason: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    checked_at: datetime = field(default_factory=datetime.now)


class SafetyEngine:
    """安全引擎：规则表 + 输入/输出审核 + 审计。"""

    def __init__(self) -> None:
        self._audit_log: list[SafetyEvent] = []
        self._enable_input_check = True
        self._enable_output_check = True
        self._enable_repeat_detection = True
        # 幻觉词表（"可能""不确定"）在医疗回答里出现频率极高，默认关闭，
        # 否则会把大量正常回答误判成低可信；需要时由管理端显式开启。
        self._enable_hallucination_check = False

        self._rules: list[RiskRule] = [
            RiskRule(
                rule_id="input_sensitive",
                rule_name="输入敏感内容",
                category="content",
                layer="input",
                action=SafetyAction.BLOCK,
                risk_level=RiskLevel.HIGH,
                patterns=SENSITIVE_WORDS,
                advice="本次提问包含平台禁止讨论的内容。",
                reply="这个问题不在医疗问诊范围内，换个健康相关的问题吧。",
            ),
            RiskRule(
                rule_id="input_scope",
                rule_name="超出医疗问诊范围",
                category="scope",
                layer="input",
                action=SafetyAction.BLOCK,
                risk_level=RiskLevel.MEDIUM,
                patterns=OUT_OF_SCOPE_PATTERNS,
                advice="仅支持医疗健康相关的咨询。",
                reply="我目前只能回答医疗健康相关的问题，比如症状、用药、检查或挂号。",
            ),
            RiskRule(
                rule_id="output_hallucination",
                rule_name="输出可信度低",
                category="quality",
                layer="output",
                action=SafetyAction.BLOCK,
                risk_level=RiskLevel.HIGH,
                patterns=HALLUCINATION_MARKERS,
                advice="回答含有不确定表述，建议以线下医生面诊意见为准。",
            ),
            RiskRule(
                rule_id="output_redact",
                rule_name="输出敏感内容脱敏",
                category="content",
                layer="output",
                action=SafetyAction.REDACT,
                risk_level=RiskLevel.MEDIUM,
                patterns=SENSITIVE_WORDS,
                advice="回答中的敏感片段已脱敏。",
            ),
            RiskRule(
                rule_id="repeat_detection",
                rule_name="重复调用打转检测",
                category="behavior",
                layer="tool",
                action=SafetyAction.BLOCK,
                risk_level=RiskLevel.MEDIUM,
                patterns=[],
                advice="检测到工具被重复调用，已中止本轮。",
                reply="这个问题我还没想清楚，换个说法再问一次可以吗？",
            ),
        ]

    # ── 规则开关 ────────────────────────────────────────────────────────
    def _rule_enabled(self, rule: RiskRule) -> bool:
        if rule.layer == "input":
            return self._enable_input_check
        if rule.layer == "output":
            if rule.rule_id == "output_hallucination":
                return self._enable_output_check and self._enable_hallucination_check
            return self._enable_output_check
        return self._enable_repeat_detection

    def list_rules(self) -> list[dict[str, Any]]:
        """规则库快照，供管理端展示与评审。"""
        return [r.as_dict(self._rule_enabled(r)) for r in self._rules]

    # ── 审核 ────────────────────────────────────────────────────────────
    def _judge(self, text: str, layer: str) -> SafetyVerdict:
        """按层匹配规则，取最重动作作为结论。同步方法：无 IO，不能阻塞事件循环。"""
        text = text or ""
        hits: list[tuple[RiskRule, str]] = []
        for rule in self._rules:
            if rule.layer != layer or not self._rule_enabled(rule):
                continue
            term = rule.match(text)
            if term is not None:
                hits.append((rule, term))

        if not hits:
            return SafetyVerdict(action=SafetyAction.ALLOW.value, level=RiskLevel.LOW.value)

        # 多条命中时按动作严重度取主规则，等级随之取最高的
        primary, term = max(hits, key=lambda h: _ACTION_RANK[h[0].action])
        level = max((h[0].risk_level for h in hits), key=lambda l: list(RiskLevel).index(l))
        return SafetyVerdict(
            action=primary.action.value,
            level=level.value,
            matches=True,
            rule_ids=[h[0].rule_id for h in hits],
            primary=primary,
            reply=primary.reply,
            details={"matched_term": term},
        )

    def check_input(self, text: str, user_id: str = "") -> SafetyVerdict:
        """输入审核：敏感词、越权检查。"""
        verdict = self._judge(text, "input")
        if verdict.matches:
            logger.warning(
                f"输入审核命中 user={user_id or '-'} rules={verdict.rule_ids} "
                f"level={verdict.level} text={text[:30]}"
            )
        return verdict

    def review_output(self, text: str, emergency: bool = False) -> SafetyVerdict:
        """输出审核：脱敏、低可信拦截。

        emergency=True 表示本轮是急症咨询：不改判定动作，但在安全提示里追加
        线下就医建议（急症场景下模型措辞的可信度要求更高）。
        """
        verdict = self._judge(text, "output")
        if emergency:
            verdict.details["emergency"] = True
            advice = verdict.primary.advice if verdict.primary else ""
            suffix = "如症状加重请立即就医或拨打 120。"
            if advice and not advice.endswith(suffix):
                if verdict.primary is not None:
                    # frozen dataclass，重建一条替换 advice，避免就地改写常量规则
                    verdict.primary = RiskRule(
                        rule_id=verdict.primary.rule_id,
                        rule_name=verdict.primary.rule_name,
                        category=verdict.primary.category,
                        layer=verdict.primary.layer,
                        action=verdict.primary.action,
                        risk_level=verdict.primary.risk_level,
                        patterns=verdict.primary.patterns,
                        advice=advice + suffix,
                        reply=verdict.primary.reply,
                    )
            elif not advice:
                verdict.details["advice"] = suffix
        if verdict.matches:
            logger.warning(
                f"输出审核命中 rules={verdict.rule_ids} level={verdict.level} "
                f"emergency={emergency} chars={len(text or '')}"
            )
        return verdict

    def finalize_output(self, text: str, verdict: SafetyVerdict) -> str:
        """按审核结论处理正文：阻断→兜底话术，脱敏→打码，其余原样返回。"""
        text = text or ""
        if verdict.is_blocking:
            return verdict.reply or "抱歉，这段内容我无法提供，建议咨询线下医生确认。"
        if verdict.action == SafetyAction.REDACT.value:
            for word in SENSITIVE_WORDS:
                text = text.replace(word, REDACT_MASK)
        return text

    def check_repeat(self, call_history: list[dict], user_id: str = "") -> SafetyResult:
        """打转检测：同一工具在一轮里被反复调用即中止。"""
        if not self._enable_repeat_detection:
            return SafetyResult(action=SafetyAction.ALLOW)

        recent_calls = call_history[-3:] if len(call_history) >= 3 else call_history
        if len(recent_calls) < 2:
            return SafetyResult(action=SafetyAction.ALLOW)

        tool_names = [c.get("tool_name", "") for c in recent_calls]
        if len(tool_names) != len(set(tool_names)):
            rule = next(r for r in self._rules if r.rule_id == "repeat_detection")
            self._block(user_id=user_id, risk_level=rule.risk_level,
                        reason="检测到重复调用，已中止", layer=rule.rule_id)
            return SafetyResult(
                action=SafetyAction.BLOCK,
                risk_level=rule.risk_level,
                reason="检测到重复调用，已中止",
                details={"layer": rule.rule_id},
            )

        return SafetyResult(action=SafetyAction.ALLOW)

    # ── 审计 ────────────────────────────────────────────────────────────
    def _block(self, user_id: str, risk_level: RiskLevel, reason: str, layer: str) -> None:
        self._audit_log.append(
            SafetyEvent(
                event_type=layer,
                user_id=user_id,
                risk_level=risk_level,
                action=SafetyAction.BLOCK,
                reason=reason,
            )
        )
        logger.warning(f"安全阻断: {reason} user={user_id or '-'} layer={layer}")

    def record_event(self, event: SafetyEvent) -> None:
        """外部（API 层）把一次命中写入审计。"""
        self._audit_log.append(event)
        # 审计只在内存里，长期跑会无限增长，保留最近 200 条
        if len(self._audit_log) > 200:
            del self._audit_log[: len(self._audit_log) - 200]

    def get_audit_log(self, limit: int = 100) -> list[SafetyEvent]:
        return self._audit_log[-limit:]


_safety_engine: SafetyEngine | None = None


def get_safety_engine() -> SafetyEngine:
    global _safety_engine
    if _safety_engine is None:
        _safety_engine = SafetyEngine()
    return _safety_engine


def record_safety_event(
    verdict: SafetyVerdict,
    *,
    stage: str,
    user_id: str = "",
    session_id: str = "",
    text: str = "",
    request_id: str = "",
) -> None:
    """记录一次安全审核命中。

    只落结构化字段与命中词，不落原文（问诊内容含健康隐私信息，不进日志）。
    """
    logger.warning(
        f"安全事件 stage={stage} user={user_id or '-'} action={verdict.action} "
        f"level={verdict.level} rules={','.join(verdict.rule_ids) or '-'} "
        f"session={session_id or '-'} rid={request_id or '-'} chars={len(text or '')}"
    )
    engine = get_safety_engine()
    try:
        level = RiskLevel(verdict.level)
    except ValueError:
        level = RiskLevel.MEDIUM
    try:
        action = SafetyAction(verdict.action)
    except ValueError:
        action = SafetyAction.WARN
    engine.record_event(
        SafetyEvent(
            event_type=f"{stage}:{','.join(verdict.rule_ids) or 'unknown'}",
            user_id=user_id,
            risk_level=level,
            action=action,
            reason=verdict.primary.advice if verdict.primary else "",
        )
    )
