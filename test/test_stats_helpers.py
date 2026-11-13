# 统计接口的纯逻辑单测。
# 时间条件此前用字符串拼接（f"{end} 23:59:59"），既走不了索引也会漏数据。

from datetime import datetime

import pytest

from src.api.routers.stats import _parse_moment


def test_parse_moment_accepts_date_only():
    assert _parse_moment("2026-10-28") == datetime(2026, 10, 28)


def test_parse_moment_accepts_datetime():
    assert _parse_moment("2026-10-28 08:30:00") == datetime(2026, 10, 28, 8, 30)


def test_parse_moment_returns_none_for_empty():
    assert _parse_moment(None) is None
    assert _parse_moment("") is None


def test_parse_moment_passes_through_unparsable_values():
    # 无法识别的格式原样交给数据库判断，而不是在应用层悄悄吞掉
    assert _parse_moment("yesterday") == "yesterday"


def test_parse_moment_result_can_build_next_day_boundary():
    from datetime import timedelta

    end = _parse_moment("2026-10-28")
    assert end + timedelta(days=1) == datetime(2026, 10, 29)  # 次日零点做开区间
