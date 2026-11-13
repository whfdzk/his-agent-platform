# src/core/redaction.py
#
# 日志 / 追踪 / 审计的脱敏工具。
#
# 硬性要求：日志与监控不得输出完整病历、身份证号、手机号、报告原文。
# 所有写入日志、trace attributes、safety snippet 的文本都必须先经过这里。
#
# 与 src/utils/masking.py 的区别：masking 面向"要展示给用户的脱敏值"
# （保留首尾便于核对），本模块面向"完全不能出网的日志"，默认整段替换。

from __future__ import annotations

import re

# 身份证（18 位，含末位 X）/ 手机号 / 邮箱 / 银行卡
_ID_CARD_RE = re.compile(r"\d{6}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx]")
_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_BANK_CARD_RE = re.compile(r"(?<!\d)\d{16,19}(?!\d)")

# 这些键的值一律不进入日志/追踪（可能是病历原文、报告全文、凭证）
SENSITIVE_KEYS = frozenset({
    "content", "text", "message", "question", "answer", "reply", "prompt",
    "raw_text", "full_text", "report_text", "ocr_text", "diagnosis", "prescription",
    "chief_complaint", "medical_history", "allergy_history", "surgery_history",
    "family_history", "id_card", "phone", "password", "password_hash", "token",
    "api_key", "authorization", "secret", "object_key",
})


def redact_phi(text: str | None) -> str:
    """只做 PHI 正则替换，不截断。

    用于"要送给模型但必须先去掉可直接识别身份的信息"的场景：
    截断会破坏临床语义（如把一句主诉砍半），因此与 redact_text 分开。
    """
    if not text:
        return ""
    cleaned = _ID_CARD_RE.sub("[身份证]", text)
    cleaned = _PHONE_RE.sub("[手机号]", cleaned)
    cleaned = _EMAIL_RE.sub("[邮箱]", cleaned)
    cleaned = _BANK_CARD_RE.sub("[卡号]", cleaned)
    return cleaned


def redact_text(text: str | None, max_len: int = 200) -> str:
    """脱敏并截断文本：正则替换 PHI，超长内容只留前 max_len 字。"""
    if not text:
        return ""
    cleaned = redact_phi(text).replace("\r", " ").replace("\n", " ").strip()
    if len(cleaned) > max_len:
        return cleaned[:max_len] + "…"
    return cleaned


def redact_payload(value, _depth: int = 0):
    """递归脱敏 dict/list：敏感键的值替换为标记，字符串做正则脱敏。"""
    if _depth > 4:
        return "<truncated>"
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if str(k).lower() in SENSITIVE_KEYS:
                out[k] = "<redacted>"
            else:
                out[k] = redact_payload(v, _depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [redact_payload(v, _depth + 1) for v in value[:50]]
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return redact_text(str(value))


def contains_phi(text: str) -> bool:
    """是否含有身份证/手机号（用于自检与测试）。"""
    if not text:
        return False
    return bool(
        _ID_CARD_RE.search(text) or _PHONE_RE.search(text) or _EMAIL_RE.search(text)
    )
