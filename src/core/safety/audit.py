# src/core/safety/audit.py
#
# 安全事件审计落库（写 safety_events）。
#
# 只允许写脱敏片段（≤200 字），不写完整消息、不写病历原文。
# 落库走批量队列，审核失败不影响主流程。

from __future__ import annotations

from loguru import logger

from src.core.persist_queue import PersistItem, enqueue
from src.core.redaction import redact_text
from src.core.langfuse_tracing import current_trace

# 需要人工复核的处置动作
_REVIEW_ACTIONS = {"block", "escalate"}


def record_safety_event(
    verdict,
    *,
    stage: str,
    user_id: str,
    session_id: str,
    text: str = "",
    request_id: str = "",
) -> None:
    """把一次审核结论写入安全事件表，并挂到当前 trace 上。"""
    if verdict is None:
        return

    trace = current_trace()
    trace_id = trace.trace_id if trace else ""
    if trace is not None:
        trace.set_safety(verdict.level, verdict.action, verdict.rule_ids)
        span = trace.start_span(
            "safety.review", "safety",
            attributes={
                "stage": stage,
                "level": verdict.level,
                "action": verdict.action,
                "rules": verdict.rule_ids,
            },
        )
        trace.end_span(span, status="blocked" if verdict.is_blocking else "ok")

    primary = verdict.primary
    requires_review = bool(verdict.requires_review or verdict.action in _REVIEW_ACTIONS)
    payload = {
        "trace_id": trace_id,
        "request_id": request_id or (trace.request_id if trace else ""),
        "user_id": user_id,
        "session_id": session_id,
        "stage": stage,
        "level": verdict.level,
        "action": verdict.action,
        "rule_id": primary.id if primary else "",
        "rule_name": primary.name if primary else "",
        "category": primary.category if primary else "general",
        "snippet": redact_text(text, 200) if text else "",
        "requires_review": requires_review,
        "review_status": "pending" if requires_review else "none",
    }
    try:
        enqueue(PersistItem(kind="safety_event", payload=payload))
    except Exception as e:
        logger.warning(f"safety_event 入队失败（不影响主流程）: {e}")

    if verdict.level in ("high", "critical"):
        logger.bind(audit=True).warning(
            f"安全事件 stage={stage} level={verdict.level} action={verdict.action} "
            f"rules={verdict.rule_ids} review={requires_review}"
        )
