# src/modules/handoff/middleware.py
#
# C3 人工接管：把"转人工"落成真正的图挂起（LangGraph interrupt）。
#
# 为什么用中间件而不是在工具里 interrupt：
#   1. 需要在"模型决定转人工"与"工具执行"之间插入建单 —— 建单必须先于挂起，
#      否则坐席台在挂起期间什么都看不到，患者等的是一个空队列；
#   2. 恢复时要注入坐席的原话作为工具结果，而不是让工具再跑一遍（它会重复建单）；
#   3. 官方 HumanInTheLoopMiddleware 就是在 after_model 里调 interrupt()，
#      这条路径在本项目的 create_agent 装配下是框架支持的，不需要魔改图。
#
# 挂起后：这一轮 SSE 到此为止（模型不会再说话）；
# 恢复时：cmd = Command(resume={"reply": ..., "resolution": ..., "agent": ...})
#         → aafter_model 重跑 → 建单命中幂等键 → interrupt() 返回坐席内容
#         → 用 ToolMessage 把坐席原话交给模型 → 模型生成收尾话术。

from __future__ import annotations

from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, ToolMessage
from langgraph.types import interrupt
from loguru import logger

from src.tools.handoff_tools import HANDOFF_TOOL_NAME


def _interrupt_payload(ticket: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "human_handoff",
        "ticket_id": ticket.get("id"),
        "ticket_no": ticket.get("ticket_no"),
        "status": ticket.get("status"),
        "summary": ticket.get("summary"),
        "patient_id": ticket.get("patient_id"),
        "session_id": ticket.get("session_id"),
    }


class HandoffInterruptMiddleware(AgentMiddleware):
    """模型调用 request_human_handoff 时：建单 → interrupt() 挂起 → 坐席回复后恢复。"""

    name = "handoff_interrupt"

    _NOTE_PREFIX = "【人工坐席处理结果（系统记录）】"

    async def abefore_agent(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        """进图前清理"孤儿工具结果"。

        为什么需要：恢复时 `_drop_call` 把 AIMessage 里的 tool_calls 摘掉了
        （防止 ToolNode 再跑一遍），但留下了一条 ToolMessage。这条消息在
        checkpoint 里就没有"带 tool_calls 的前置 assistant 消息"了，下一轮把
        整段历史发给模型时，OpenAI 兼容网关会直接 400：
        `messages with role "tool" must be a response to a preceding message with "tool_calls"`。
        （实测踩到：转人工后紧接着再问一句 → 整轮报"服务暂时不可用"）

        处理：把这类 ToolMessage 换成一条带标记的人类消息，保留上下文又不违反协议。
        """
        messages = (state or {}).get("messages") or []
        live_ids: set[str] = set()
        updates: list[Any] = []
        for msg in messages:
            if isinstance(msg, AIMessage):
                for call in (getattr(msg, "tool_calls", None) or ()):
                    live_ids.add(str(call.get("id") or ""))
            elif isinstance(msg, ToolMessage):
                if str(msg.tool_call_id or "") in live_ids:
                    continue
                updates.append(RemoveMessage(id=msg.id))
                updates.append(HumanMessage(
                    content=f"{self._NOTE_PREFIX}{msg.content}",
                ))
        if not updates:
            return None
        logger.info(f"清理孤儿工具结果 {len(updates) // 2} 条（避免下轮 400 tool_use 不配对）")
        return {"messages": updates}

    async def aafter_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        messages = (state or {}).get("messages") or []
        if not messages:
            return None
        last_ai = next((m for m in reversed(messages) if isinstance(m, AIMessage)), None)
        if last_ai is None or not getattr(last_ai, "tool_calls", None):
            return None

        handoff_calls = [c for c in last_ai.tool_calls if c.get("name") == HANDOFF_TOOL_NAME]
        if not handoff_calls:
            return None

        # 一次只处理第一个转人工调用：同一轮里调两次只会重复建单
        call = handoff_calls[0]
        args = call.get("args") or {}
        call_id = str(call.get("id") or "")

        ctx = getattr(runtime, "context", None)
        user_id = str(getattr(ctx, "user_id", "") or "")
        session_id = str(getattr(ctx, "session_id", "") or "")
        if not session_id:
            configurable = (getattr(runtime, "config", None) or {}).get("configurable", {})
            thread_id = str(configurable.get("thread_id", "") or "")
            session_id = thread_id.split(":", 1)[1] if ":" in thread_id else ""

        # 游客 / 身份异常：不建单、不挂起，直接让模型把话说明白
        if not user_id.isdigit() or not session_id:
            return self._drop_call(last_ai, handoff_calls, ToolMessage(
                content="当前为未登录（游客）状态或会话信息缺失，无法转人工。"
                        "请提示患者登录后再申请人工坐席，不要声称已转接。",
                tool_call_id=call_id,
                name=HANDOFF_TOOL_NAME,
            ))

        ticket = await self._ensure_ticket(
            patient_id=int(user_id),
            session_id=session_id,
            summary=str(args.get("summary") or ""),
            reason=str(args.get("reason") or "patient_request"),
            priority=str(args.get("priority") or "normal"),
            dedupe_key=f"handoff:{call_id}",
            tenant_id=int(getattr(ctx, "tenant_id", 1) or 1),
        )
        if ticket is None:
            return self._drop_call(last_ai, handoff_calls, ToolMessage(
                content="转人工建单失败（系统繁忙）。请如实告知患者稍后再试，不要声称已转接。",
                tool_call_id=call_id,
                name=HANDOFF_TOOL_NAME,
            ))

        # ── 挂起点：本轮到此为止 ───────────────────────────────────────
        resume_value = interrupt(_interrupt_payload(ticket))

        # ── 恢复：坐席已经处理过这张单 ────────────────────────────────
        if isinstance(resume_value, dict):
            reply = str(resume_value.get("reply") or "").strip()
            resolution = str(resume_value.get("resolution") or "").strip()
            agent_name = str(resume_value.get("agent") or "坐席")
            cancelled = bool(resume_value.get("cancelled"))
        else:  # 兜底：resume 值不是 dict（例如手工调试传了字符串）
            reply, resolution, agent_name, cancelled = str(resume_value or ""), "", "坐席", False

        if cancelled:
            content = (
                f"【人工接管已取消】患者撤回了人工请求，工单 {ticket['ticket_no']} 已取消，"
                "坐席不会再介入。这一轮到此结束。"
            )
        else:
            parts = [f"【人工坐席 {agent_name} 已接手处理工单 {ticket['ticket_no']}】"]
            if reply:
                parts.append(f"坐席原话：{reply}")
            if resolution:
                parts.append(f"处理说明：{resolution}")
            parts.append("人工接管到此结束，后续对话由你继续。")
            content = "\n".join(parts)

        logger.info(
            f"人工接管恢复：工单 {ticket['ticket_no']} patient={ticket['patient_id']} "
            f"agent={agent_name} cancelled={cancelled}"
        )
        return self._drop_call(last_ai, handoff_calls, ToolMessage(
            content=content, tool_call_id=call_id, name=HANDOFF_TOOL_NAME,
        ))

    @staticmethod
    def _drop_call(last_ai: AIMessage, calls: list[dict], tool_message: ToolMessage) -> dict[str, Any]:
        """把这几个转人工调用从 AIMessage 里摘掉，并补一条工具结果。

        摘掉的原因：调用已经被中间件处理（建单/挂起/恢复），不能再让 ToolNode
        执行工具本体 —— 那会第二次建单，或者在恢复后重新挂起一次。
        """
        drop_ids = {str(c.get("id") or "") for c in calls}
        last_ai.tool_calls = [c for c in last_ai.tool_calls if str(c.get("id") or "") not in drop_ids]
        return {"messages": [last_ai, tool_message]}

    @staticmethod
    async def _ensure_ticket(
        *, patient_id: int, session_id: str, summary: str, reason: str,
        priority: str, dedupe_key: str, tenant_id: int = 1,
    ) -> dict[str, Any] | None:
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
                    dedupe_key=dedupe_key,
                    payload={"source": "supervisor_tool"},
                    tenant_id=tenant_id,
                )
                info = handoff_service.serialize(ticket)
                info["created"] = created
                return info
        except Exception as e:  # noqa: BLE001
            logger.warning(f"人工接管建单失败 patient={patient_id}: {type(e).__name__}: {e}")
            return None
