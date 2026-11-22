# src/tools/handoff_tools.py
#
# C3 人工接管：模型侧"转人工"工具。
#
# 为什么要有这个工具：患者说"我要转人工""能不能找真人""你们有没有客服"时，
# 光靠提示词让模型嘴上说"已为您转接"是假的 —— 后台没有工单，坐席台看不到人，
# 患者等到的是一个永远不会来的回答。
#
# 真实链路（supervisor 装配了 HandoffInterruptMiddleware）：
#   模型调用本工具 → 中间件在 after_model 里先建单，再 interrupt() 把整轮挂起
#   → 患者收到"已转人工 + 工单号"；坐席认领/回复/结单后
#   → Command(resume=...) 让这一轮从挂起点继续，模型拿着坐席的回复收尾。
#
# 因此本函数体在 supervisor 链路上**不会被执行**（中断发生在它之前）。
# 保留可执行实现是为了两种情况：其他未装配中间件的 Agent 也能用；
# 以及中间件被关掉时工具仍有正常语义（建单 + 返回工单号）。

from __future__ import annotations

from langchain_core.tools import tool
from langgraph.prebuilt import ToolRuntime
from loguru import logger

from src.core.context import UserContext

HANDOFF_TOOL_NAME = "request_human_handoff"


def _tenant_of(runtime: ToolRuntime) -> int:
    ctx = runtime.context if runtime is not None else None
    return int(getattr(ctx, "tenant_id", 1) or 1)


def _resolve_identity(runtime: ToolRuntime) -> tuple[int | None, str]:
    """从运行时上下文取（患者 ID, 会话 ID）。"""
    ctx = runtime.context if runtime is not None else None
    user_id = str(getattr(ctx, "user_id", "") or "")
    session_id = str(getattr(ctx, "session_id", "") or "")
    if not session_id:
        configurable = (runtime.config or {}).get("configurable", {}) if runtime else {}
        thread_id = str(configurable.get("thread_id", "") or "")
        if ":" in thread_id:
            user_id = user_id or thread_id.split(":", 1)[0]
            session_id = thread_id.split(":", 1)[1]
    patient_id = int(user_id) if user_id.isdigit() else None
    return patient_id, session_id


@tool
async def request_human_handoff(
    runtime: ToolRuntime[UserContext],
    summary: str,
    reason: str = "patient_request",
    priority: str = "normal",
) -> str:
    """
    把当前对话转接给人工坐席（真人医生/客服），并生成一张工单。

    使用时机（满足任意一条就调用，不要自己编"已转接"）：
    - 患者明确要求转人工：说"转人工""找真人""人工客服""能不能有人看看"；
    - 患者反复表示同一个问题没解决，你已经给过建议但仍无效；
    - 涉及需要人工审核/线下处置、你无法确认的事项（如具体处方调整、投诉、退费）。

    summary 写清患者的诉求（一句话，不要贴整段对话）。
    reason 取值：patient_request（患者要求）/ model_escalate（模型判断接不住）/
    emergency（急症需人工介入）/ unclear（意图不清）。
    priority 取值：low / normal / high / urgent（急症、投诉用 high 或 urgent）。

    调用后本轮会被挂起，患者会看到"已转人工 + 工单号"；坐席回复后你会拿到坐席的
    原话，届时只需一句话确认患者已看到，不要复述坐席内容。
    """
    patient_id, session_id = _resolve_identity(runtime)
    if patient_id is None:
        return "当前为未登录（游客）状态，无法转人工。请提示患者登录后再申请人工坐席。"
    if not session_id:
        return "当前会话信息缺失，无法建单。请让患者刷新页面后重试。"

    try:
        from src.infra.database import AsyncSessionLocal
        from src.modules.handoff import service as handoff_service

        async with AsyncSessionLocal() as db:
            ticket, created = await handoff_service.create_ticket(
                db,
                patient_id=patient_id,
                session_id=session_id,
                reason=reason,
                priority=priority,
                summary=summary,
                tenant_id=_tenant_of(runtime),
            )
            info = handoff_service.serialize(ticket)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"转人工建单失败 patient={patient_id}: {type(e).__name__}: {e}")
        return "转人工暂时不可用（系统繁忙）。请如实告知患者稍后再试，不要声称已转接。"

    return (
        f"已为患者创建人工工单 {info['ticket_no']}（当前状态 {info['status']}）。"
        "请告知患者：坐席会尽快接入，接入前可以继续描述情况；"
        "不要编造坐席的回复内容。"
        + ("" if created else "（该会话已有未结束工单，已复用，不要重复建单。）")
    )
