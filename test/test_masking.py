# PHI 脱敏工具的确定性单测：脱敏结果绝不能还原出原始值。

import pytest

from src.utils.masking import mask_id_card, mask_name, mask_phone


@pytest.mark.parametrize("raw, expected", [
    ("13812345678", "138****5678"),
    ("1381234", "138****1234"),
])
def test_mask_phone_keeps_prefix_and_suffix(raw: str, expected: str):
    assert mask_phone(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "123456"])
def test_mask_phone_degrades_to_placeholder(raw):
    assert mask_phone(raw) == "***"


def test_mask_id_card_keeps_four_at_each_end():
    assert mask_id_card("110101199003071234") == "1101**********1234"


@pytest.mark.parametrize("raw", [None, "", "1101"])
def test_mask_id_card_degrades_to_placeholder(raw):
    assert mask_id_card(raw) == "***"


@pytest.mark.parametrize("raw, expected", [
    ("张晓晓", "张**"),
    ("张三", "张*"),
    ("张", "张*"),
])
def test_mask_name_keeps_only_first_char(raw: str, expected: str):
    assert mask_name(raw) == expected


def test_mask_name_degrades_to_placeholder():
    assert mask_name(None) == "***"


@pytest.mark.parametrize("raw", [
    "13812345678",
    "110101199003071234",
    "张晓晓",
])
def test_masked_value_never_equals_raw(raw: str):
    for masked in (mask_phone(raw), mask_id_card(raw), mask_name(raw)):
        assert masked != raw
