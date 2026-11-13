# src/tools/runtime.py
#
# 工具层的身份解析：所有"当前登录患者"类工具共用同一套规则。
#
# 规则（与 store_tools 一致）：
# - 优先取 ToolRuntime.context（UserContext），回退从 configurable.thread_id 解析
# - thread_id 形如 "{user_id}:{session_id}"
# - 游客（anon / 非数字）返回 None，由各工具自行给出"登录后可xxx"的提示

from __future__ import annotations

from langgraph.prebuilt import ToolRuntime

GUEST_HINT = "当前为未登录（游客）状态，该功能需要登录后使用；请提示用户登录。"


def resolve_user_id(runtime: ToolRuntime) -> str:
    """解析 user_id；解析不到返回空串（调用方按游客处理）。"""
    ctx = getattr(runtime, "context", None)
    if ctx is not None and getattr(ctx, "user_id", None):
        return str(ctx.user_id)
    configurable = runtime.config.get("configurable", {}) if runtime.config else {}
    thread_id = configurable.get("thread_id", "") or ""
    return thread_id.split(":")[0] if thread_id else ""


def resolve_patient_id(runtime: ToolRuntime) -> int | None:
    """解析 patients.id；游客/非法 ID 返回 None。"""
    user_id = resolve_user_id(runtime)
    if not user_id or user_id == "anon" or not user_id.isdigit():
        return None
    return int(user_id)
