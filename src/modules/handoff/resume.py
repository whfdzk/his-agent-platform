# src/modules/handoff/resume.py
#
# C3 人工接管：坐席处理完 → 恢复被挂起的那一轮对话。
#
# 语义：
#   挂起点在 supervisor 图的 after_model（HandoffInterruptMiddleware）。
#   坐席回复/结单/患者撤回时，用 Command(resume={...}) 把同一个 thread 续跑：
#   模型拿到"坐席已回复 xxx"，生成一句收尾话术（不重复坐席内容），
#   这段文字写回 chat_messages（event_type=handoff_close），患者下次刷新即可看到。
#
# 三条防御（都不是假想）：
#   1. 没有 pending interrupt 就别 resume —— 患者是点"转人工"按钮建的单（没走图），
#      对这样的 thread 发 Command 会直接把图跑飞或报错；
#   2. resume 失败不能影响工单状态 —— 工单已结是事实，收尾话术是锦上添花；
#   3. 模型生成的收尾话术照样过输出安全审核，不能因为换了条链路就绕过合规。

from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger

from src.modules.handoff.model import HandoffTicket


def thread_id_of(ticket: HandoffTicket) -> str:
    """"患者ID:会话ID" —— 与 chat 路由 _thread_id 完全一致，不能各自拼串。"""
    return f"{int(ticket.patient_id)}:{str(ticket.session_id or '')}"


def _pending_handoff_interrupt(state: Any) -> bool:
    """该 thread 上是否正停着一个"转人工"挂起点。"""
    for task in getattr(state, "tasks", ()) or ():
        for it in getattr(task, "interrupts", ()) or ():
            value = getattr(it, "value", None)
            if isinstance(value, dict) and value.get("kind") == "human_handoff":
                return True
    return False


async def resume_handoff(
    ticket: HandoffTicket,
    *,
    reply: str = "",
    resolution: str = "",
    agent_name: str = "",
    cancelled: bool = False,
    timeout: float = 30,
) -> str:
    """续跑挂起的一轮，返回模型生成的收尾话术（没有可续跑的挂起则返回空串）。"""
    try:
        from langgraph.types import Command

        from src.agents.supervisor_agent import get_supervisor_agent
        from src.core.context import UserContext

        agent = await get_supervisor_agent()
        config = {"configurable": {"thread_id": thread_id_of(ticket)}}
        state = await agent.aget_state(config)
        if not _pending_handoff_interrupt(state):
            logger.info(f"工单 {ticket.ticket_no} 上没有待恢复的挂起点，跳过 resume")
            return ""

        patient_id = int(ticket.patient_id)
        result = await asyncio.wait_for(
            agent.ainvoke(
                Command(resume={
                    "reply": str(reply or ""),
                    "resolution": str(resolution or ""),
                    "agent": str(agent_name or "坐席"),
                    "cancelled": bool(cancelled),
                }),
                config=config,
                context=UserContext(
                    user_id=str(patient_id),
                    session_id=str(ticket.session_id or ""),
                    authenticated=True,
                    patient_id=patient_id,
                    tenant_id=int(getattr(ticket, "tenant_id", 1) or 1),
                ),
            ),
            timeout=timeout,
        )
        text = _final_text(result)
        return _review(text)
    except asyncio.TimeoutError:
        logger.warning(f"工单 {ticket.ticket_no} resume 超时（收尾话术未生成，不影响工单状态）")
        return ""
    except Exception as e:  # noqa: BLE001
        logger.warning(f"工单 {ticket.ticket_no} resume 失败: {type(e).__name__}: {e}")
        return ""


def _final_text(result: Any) -> str:
    """只取模型自己说的话（AIMessage）。

    恢复后模型不一定会再输出正文：转人工的 tool_call 已被中间件摘掉，
    create_agent 在 after_model 之后没有待执行工具就直接结束这一轮，
    messages 末尾留下的是那条 ToolMessage（内部措辞，绝不能当患者可见文案）。
    """
    messages = (result or {}).get("messages") if isinstance(result, dict) else None
    if not messages:
        return ""
    for msg in reversed(messages):
        from langchain_core.messages import AIMessage

        if not isinstance(msg, AIMessage):
            continue
        content = getattr(msg, "content", None)
        if isinstance(content, str) and content.strip():
            return content.strip()
        if isinstance(content, list):
            text = "".join(
                part.get("text", "") for part in content if isinstance(part, dict)
            ).strip()
            if text:
                return text
    return ""


def _review(text: str) -> str:
    """收尾话术同样走输出安全审核（与 chat/stream 的 done 段同口径）。"""
    if not text:
        return ""
    try:
        from src.modules.safety.engine import safety_engine

        verdict = safety_engine.review_output(text)
        return safety_engine.finalize_output(text, verdict)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"收尾话术输出审核失败（原文照发）: {type(e).__name__}: {e}")
        return text


_REASON_LEAD = {
    "patient_request": "你申请的转人工",
    "model_escalate": "系统建议转人工的这个问题",
    "emergency": "这次急诊优先处理",
    "unclear": "这条转人工请求",
}


def fallback_closing(ticket: HandoffTicket, *, cancelled: bool = False) -> str:
    """模型没产出收尾话术时的兜底文案。

    与"让模型再编一句"相比，这里只用工单上已经存在的字段拼一句实话：
    谁处理的、处理了多久、坐席在会话里回了几条、后续怎么继续。
    不引用 resolution —— 那是坐席侧的内部处理记录（"已说明叠加风险"这种口吻
    患者看不懂），回放里也不该出现。
    """
    no = str(ticket.ticket_no or "")
    if cancelled:
        return f"转人工请求已按你的要求撤回（工单 {no}），本次没有坐席介入。后续有需要可以继续在这里问我，也可以随时再次转人工。"

    lead = _REASON_LEAD.get(str(ticket.reason or ""), "这条转人工请求")
    who = str(ticket.assigned_name or "").strip()
    tail = "后续还有问题可以直接在这里问我；需要继续找真人，也可以再点一次转人工。"

    parts: list[str] = []
    parts.append(f"人工坐席{who}已处理完{lead}（工单 {no}）。" if who else f"{lead}已处理完毕（工单 {no}）。")

    # 处理时长：只有真正被认领过才有意义（接口建单直接结单的情况 claimed_at 为空）
    if ticket.claimed_at and ticket.resolved_at:
        minutes = int((ticket.resolved_at - ticket.claimed_at).total_seconds() // 60)
        parts.append(f"本次人工处理用时约 {minutes} 分钟。" if minutes >= 1 else "本次人工处理不到 1 分钟。")

    replies = int(ticket.message_count or 0)
    if replies > 0:
        parts.append(f"坐席在会话里回复了 {replies} 条。")

    parts.append(tail)
    return "".join(parts)


async def persist_closing(ticket: HandoffTicket, text: str) -> None:
    """把收尾话术写回患者会话。"""
    if not str(text or "").strip():
        return
    try:
        from src.infra.database import AsyncSessionLocal
        from src.modules.handoff import service as handoff_service

        async with AsyncSessionLocal() as db:
            await handoff_service.append_session_message(
                db, ticket=ticket, content=text, role="assistant", event_type="handoff_close",
            )
            await db.commit()
            # 结单/撤单的收尾话术也要让患者端立刻看到：
            # 光落库的话，患者要刷新页面或等下一轮提问才发现"人工环节结束了"。
            await handoff_service.broadcast_ticket(
                ticket,
                message={
                    "ticket_no": str(ticket.ticket_no or ""),
                    "content": str(text),
                    "event_type": "handoff_close",
                    "message_count": int(ticket.message_count or 0),
                },
            )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"工单 {ticket.ticket_no} 收尾话术落库失败: {type(e).__name__}: {e}")
