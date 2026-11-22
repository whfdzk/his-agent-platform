# src/modules/clinical/dates.py
#
# 预约日期规范化：把模型/用户的口语日期收敛成 YYYY-MM-DD。
#
# 为什么要这一层：实测「帮我挂个心内科，明天上午的号」被模型换算成 2023-11-22，
# 草稿、确认卡片、最终预约都显示错误的年份。相对日期不能让模型心算，工具层用规则算；
# 算不出来就明确要求重新提供，绝不把错的日期写进库里。

from __future__ import annotations

import re
from datetime import date, timedelta

_WEEKDAYS = {
    "一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6, "七": 6,
}

# 2026-11-25 / 2026/11/25 / 2026年11月25日
_FULL_DATE = re.compile(r"(\d{4})[-/年.](\d{1,2})[-/月.](\d{1,2})")
# 11月25日
_MONTH_DAY = re.compile(r"(\d{1,2})月(\d{1,2})[日号]")
# 周三 / 本周三 / 这周三 / 下周三 / 星期日 / 礼拜五
_WEEK = re.compile(r"(本|这|下)?(?:周|星期|礼拜)([一二三四五六日天七])")


def normalize_date(raw: str, *, today: date | None = None) -> tuple[str | None, str]:
    """把口语日期转成 YYYY-MM-DD。

    Returns:
        (成功: "YYYY-MM-DD", ""), (失败: None, 给模型看的原因)
    """
    t = (raw or "").strip()
    if not t:
        return None, "缺少就诊日期"
    today = today or date.today()

    # 相对日（先判"大后天"，避免被"后天"截胡）
    if any(w in t for w in ("今天", "今日", "当日")):
        return today.isoformat(), ""
    if "大后天" in t:
        return (today + timedelta(days=3)).isoformat(), ""
    if "后天" in t:
        return (today + timedelta(days=2)).isoformat(), ""
    if any(w in t for w in ("明天", "明日", "次日")):
        return (today + timedelta(days=1)).isoformat(), ""

    m = _FULL_DATE.search(t)
    if m:
        try:
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None, f"日期不合法：{m.group(0)}"
        if d < today:
            return None, f"{d.isoformat()} 已经过去了"
        return d.isoformat(), ""

    m = _WEEK.search(t)
    if m:
        prefix = m.group(1) or ""
        weekday = _WEEKDAYS[m.group(2)]
        monday = today - timedelta(days=today.weekday())
        d = monday + timedelta(days=weekday)
        if prefix == "下":
            d += timedelta(days=7)
        elif d < today:
            # 「本周三」但今天已是周五 —— 口语里指的是下周三，不是过去那天
            d += timedelta(days=7)
        return d.isoformat(), ""

    m = _MONTH_DAY.search(t)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        for year in (today.year, today.year + 1):
            try:
                d = date(year, month, day)
            except ValueError:
                return None, f"日期不合法：{m.group(0)}"
            if d >= today:
                return d.isoformat(), ""
        return None, f"日期不合法：{m.group(0)}"

    return None, f"无法识别的日期：{t}。请按 YYYY-MM-DD 或「明天上午」这类说法重新提供"
