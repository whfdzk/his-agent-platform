# src/core/safety/repeat.py
#
# 打转（agent 原地循环）判定的纯函数层。
#
# 为什么按「工具名 + 参数」而不是「工具名」判重：
# - 同名工具换个参数就是有效进展（连查三个不同症状 → 症状表判重会把正常排查误杀）
# - 真正烧预算的是"同一组参数被反复提交"，模型拿不到新信息还在原地打转
#
# 放在 core 而不放 agents：调用它的既有 safety engine（core），也有 supervisor 中间件
# （agents）。core 不能 import agents（见 test_layering.py），所以纯逻辑必须落在最底层。

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable, Mapping, Sequence

# 窗口：最近 N 次调用之内出现同一指纹即算打转
DEFAULT_WINDOW = 3

# 豁免工具：本来就该反复调用的（记忆检索 / 档案查询 / 保存记忆）
EXEMPT_TOOLS: frozenset[str] = frozenset({
    "search_memory",
    "save_memory",
    "get_my_profile",
})


def fingerprint(tool_name: str, args: Mapping[str, Any] | None) -> str:
    """工具调用指纹：工具名 + 参数（键排序）后取 md5 前 16 位。

    参数键顺序不同、值相同 → 同一指纹（否则模型换个键序就能绕过判重）。
    参数无法 JSON 序列化时退化成 str()，宁可指纹不稳定也不能抛异常打断链路。
    """
    try:
        payload = json.dumps(args or {}, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:
        payload = str(args)
    raw = f"{tool_name}:{payload}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:16]


def find_repeats(
    calls: Sequence[Mapping[str, Any]],
    *,
    window: int = DEFAULT_WINDOW,
    exempt: Iterable[str] = EXEMPT_TOOLS,
) -> list[int]:
    """在一串调用（按先后顺序）里找出"打转"的下标。

    calls 每项形如 {"name": "search_kb", "args": {...}}，也兼容 {"tool_name": ...}。
    返回重复出现的下标（从 0 开始）；豁免工具永不返回。
    """
    exempt_set = set(exempt)
    last_index: dict[str, int] = {}
    repeats: list[int] = []

    for index, call in enumerate(calls):
        name = str(call.get("name") or call.get("tool_name") or "")
        if not name or name in exempt_set:
            continue
        args = call.get("args")
        if args is None:
            args = call.get("tool_args")
        fp = fingerprint(name, args if isinstance(args, Mapping) else None)

        prev = last_index.get(fp)
        if prev is not None and index - prev <= window:
            repeats.append(index)
        # 无论是否判重都更新，避免"每 4 步重复一次"永远抓不到
        last_index[fp] = index

    return repeats


def describe(call: Mapping[str, Any], *, step: int) -> str:
    """给模型看的一句话（中间件塞进 ToolMessage 的内容）。"""
    name = str(call.get("name") or call.get("tool_name") or "工具")
    return (
        f"【打转拦截】第 {step} 步已经调用过 {name}，且参数完全相同。"
        "不要重复同一调用：换参数（换个检索词、换个人群/时间范围）或换工具；"
        "如果确实需要再查一次，请在下一轮说明理由并同其他调用一起发起。"
    )
