# src/utils/masking.py
#
# PHI（患者隐私数据）脱敏工具：日志、审计、对外序列化统一使用。

from __future__ import annotations


def mask_phone(phone: str | None) -> str:
    """手机号脱敏：保留前 3 后 4，如 138****1234。"""
    if not phone or len(phone) < 7:
        return "***"
    return f"{phone[:3]}****{phone[-4:]}"


def mask_id_card(id_card: str | None) -> str:
    """身份证号脱敏：保留前 4 后 4，如 1101**********1234。"""
    if not id_card or len(id_card) < 8:
        return "***"
    return f"{id_card[:4]}{'*' * (len(id_card) - 8)}{id_card[-4:]}"


def mask_name(name: str | None) -> str:
    """姓名脱敏：保留首字，如 张**。"""
    if not name:
        return "***"
    if len(name) <= 1:
        return name + "*"
    return name[0] + "*" * (len(name) - 1)
