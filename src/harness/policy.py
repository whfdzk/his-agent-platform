# src/harness/policy.py
#
# Guardrail 的策略前置：工具调用在真正执行之前先过一遍规则。
#
# 与 core/safety/engine.py 的分工：
#   - safety/engine 管"文本内容"（用户输入、模型输出、重复调用行为）
#   - 这里管"工具调用这个动作本身"：调的是不是允许的工具、参数结构是否可用、
#     参数体量是否会把下游打穿。命中即拒绝，模型收到的是结构化拒绝说明，
#     而不是一次真实的下游调用。
#
# 规则刻意保持"声明式 + 可快照"：管理端 /admin/harness/stats 直接把 snapshot()
# 展示出来，评审能看懂"到底拦了什么"，而不是一团散落在各工具里的 if。

from __future__ import annotations

import json
from dataclasses import dataclass

# 明确禁止的"执行任意代码/写文件"类工具：本项目的工具集里没有，属于防御性黑名单
BLOCKED_TOOL_NAMES = frozenset({"shell", "python_repl", "execute_code", "file_write", "bash"})
# 单次工具参数上限：正常业务参数（患者 ID、症状文本、查询词）远小于这个量级，
# 超限基本等于把整段病历/文档塞给工具，既慢又容易触发下游长度错误。
MAX_ARG_CHARS = 4000


@dataclass(frozen=True)
class ToolDecision:
    allowed: bool
    code: str = ""
    reason: str = ""

    def as_dict(self) -> dict:
        return {"allowed": self.allowed, "code": self.code, "reason": self.reason}


_ALLOW = ToolDecision(allowed=True)


def check_tool_call(tool_name: str, args: object) -> ToolDecision:
    """工具调用的策略判定。纯函数、无 IO，可在任意链路里同步调用。"""
    if not isinstance(tool_name, str) or not tool_name.strip():
        return ToolDecision(False, "empty_tool", "工具名为空，调用已拒绝")
    if tool_name in BLOCKED_TOOL_NAMES:
        return ToolDecision(False, "tool_blocked", f"工具 {tool_name} 属于禁止调用的执行类工具")

    if args is None:
        args = {}
    if not isinstance(args, dict):
        return ToolDecision(False, "bad_args", "工具参数必须是对象（键值对）")

    try:
        size = len(json.dumps(args, ensure_ascii=False, default=str))
    except Exception:  # noqa: BLE001
        return ToolDecision(False, "unserializable_args", "工具参数无法序列化，调用已拒绝")
    if size > MAX_ARG_CHARS:
        return ToolDecision(
            False, "args_too_large", f"工具参数 {size} 字符，超过上限 {MAX_ARG_CHARS}",
        )
    return _ALLOW


def snapshot() -> dict:
    """策略快照：给管理端展示当前生效的规则。"""
    return {
        "blocked_tools": sorted(BLOCKED_TOOL_NAMES),
        "max_arg_chars": MAX_ARG_CHARS,
        "checks": ["empty_tool", "tool_blocked", "bad_args", "unserializable_args", "args_too_large"],
    }
