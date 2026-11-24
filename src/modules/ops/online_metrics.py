"""在线评测指标（P2）：按会话/trace 聚合的"产品侧"指标。

离线集只能回答"给定问题答得对不对"，答不了"用户用起来顺不顺"。这里补两个
线上最能反映体验、又不需要人工标注的指标：

1. 转人工率 = 有工单的会话数 / 总会话数
   - 高了说明 AI 接不住：可能是能力问题，也可能是话术把人吓走了
   - 分 reason 看（patient_request / model_escalate / emergency / unclear）能区分
     是"患者想找人说"还是"模型自己承认接不住"

2. 重复提问率 = 出现"同一句反问被问第二遍"的会话数 / 总会话数
   - 多轮里反复问"具体哪个部位、多久了"，是问诊状态没接上/澄清逻辑打转最直观的
     外部表现，离线 triage 切片失败率高时，这个指标会同步抬头
   - 判定用 assistant 相邻两条的字符相似度 ≥ 0.85 且都是问句，属于近似判定：
     只抓"几乎一字不差地又问一遍"，改写过的重复抓不到（宁可低报，不要误报）

数据来源全部是既有事实表（conversations / handoff_tickets / chat_messages），
不新增埋点、不改写库，随时可回算历史。文本只用 content_redacted（脱敏列），
避免把 PHI 带进统计链路。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.chat.model import ChatMessage, Conversation
from src.modules.handoff.model import HandoffTicket

REPEAT_SIM = 0.85          # 相邻两条 assistant 的相似度阈值
PUNCT = re.compile(r"[\s，,。.、；;：:！!？?~～\-—…\"“”'（）()\[\]【】]")


def _norm(text: str) -> str:
    return PUNCT.sub("", text or "")


def _is_question(text: str) -> bool:
    return ("？" in (text or "")) or ("?" in (text or ""))


async def metrics(db: AsyncSession, days: int = 7, max_messages: int = 20000) -> dict:
    # 表里 created_at 是 TIMESTAMP WITHOUT TIME ZONE（naive），
    # 传 aware datetime 会被 asyncpg 直接拒掉（offset-naive vs offset-aware）。
    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=max(1, days))

    conv_rows = (await db.execute(
        select(Conversation.id, Conversation.session_id).where(Conversation.created_at >= since)
    )).all()
    conv_ids = {r[0] for r in conv_rows}

    tickets = (await db.execute(
        select(HandoffTicket.session_id, HandoffTicket.reason)
        .where(HandoffTicket.created_at >= since)
    )).all()
    ticket_sessions = {r[0] for r in tickets}
    by_reason: dict[str, int] = {}
    for _, reason in tickets:
        by_reason[reason or "unknown"] = by_reason.get(reason or "unknown", 0) + 1

    rows = (await db.execute(
        select(ChatMessage.conversation_id, ChatMessage.role, ChatMessage.content_redacted, ChatMessage.seq)
        .where(ChatMessage.created_at >= since, ChatMessage.role == "assistant")
        .order_by(ChatMessage.conversation_id, ChatMessage.seq)
        .limit(max_messages)
    )).all()

    repeat_convs: set[int] = set()
    prev_conv, prev_text = None, ""
    for cid, _role, text, _seq in rows:
        cur = _norm(text or "")
        if cid == prev_conv and prev_text and cur:
            if _is_question(prev_text) and _is_question(cur):
                if SequenceMatcher(None, prev_text, cur).ratio() >= REPEAT_SIM:
                    repeat_convs.add(cid)
        prev_conv, prev_text = cid, cur

    total_convs = len(conv_ids) or len({r[0] for r in rows})
    assistant_msgs = len(rows)

    return {
        "window_days": days,
        "since": since.isoformat(timespec="seconds"),
        "conversations": total_convs,
        "assistant_messages": assistant_msgs,
        "handoff": {
            "sessions": len(ticket_sessions),
            "rate": round(len(ticket_sessions) / total_convs, 4) if total_convs else None,
            "tickets": len(tickets),
            "by_reason": by_reason,
        },
        "repeat_question": {
            "conversations": len(repeat_convs),
            "rate": round(len(repeat_convs) / (len({r[0] for r in rows}) or 1), 4),
            "threshold": REPEAT_SIM,
        },
        "note": "以会话为分母；重复提问用相邻 assistant 相似度近似，只抓近乎原句复述",
    }
