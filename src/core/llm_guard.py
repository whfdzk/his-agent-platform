# src/core/llm_guard.py
#
# LLM 出网前的隐私保护层。
#
# 背景：本系统会把用户输入直接送给第三方模型网关。若不处理，
# 身份证号、手机号、邮箱、卡号等可直接识别身份的信息会随 prompt 出境。
#
# 设计取舍：
# - 只替换"强标识符"，保留临床语义（症状、数值、时间），避免影响诊疗质量；
# - 原始文本仍按原样落库（审计与回溯需要），仅在调用模型时使用脱敏副本；
# - 年龄做区间化（60 → "60-69岁"）：年龄是准标识符，但临床上必须保留量级。

from __future__ import annotations

from src.core.redaction import redact_phi

# 聊天输入允许的长度与 API 层校验保持一致即可，这里不做截断
MAX_LLM_INPUT_CHARS = 4000


def scrub_for_llm(text: str | None) -> str:
    """把用户输入转成可安全送模型的文本：去强标识符 + 长度保护。

    注意：不要用它覆盖用户消息的落库内容 —— 存原文、发脱敏副本。
    """
    if not text:
        return ""
    cleaned = redact_phi(text)
    if len(cleaned) > MAX_LLM_INPUT_CHARS:
        cleaned = cleaned[:MAX_LLM_INPUT_CHARS]
    return cleaned


def age_band(age: int | None) -> str | None:
    """年龄区间化：返回 "60-69岁" 这类描述，None 原样返回。

    临床上需要年龄段而不是精确年龄；精确年龄 + 性别 + 地区组合可重识别。
    """
    if age is None:
        return None
    try:
        value = int(age)
    except (TypeError, ValueError):
        return None
    if value < 0:
        return None
    if value >= 90:
        return "90岁以上"
    lower = (value // 10) * 10
    return f"{lower}-{lower + 9}岁"
