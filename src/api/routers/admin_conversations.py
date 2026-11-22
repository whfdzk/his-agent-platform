# src/api/routers/admin_conversations.py
#
# 管理端：对话管理（记录查看 / 会话关闭 / 用户反馈收集查看）。
# - 只读为主：管理端不篡改消息内容，仅允许关闭异常会话
# - 反馈数据来自 knowledge_feedback（患者端 👍/👎），此处聚合展示

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import AdminIdentity, get_current_admin, require_scope
from src.core.exceptions import BizErrorCode, BizException
from src.infra.database import get_db
from src.modules.chat.model import ChatMessage, Conversation
from src.knowledge.model import KnowledgeFeedback

router = APIRouter(
    prefix="/api/v1/admin/conversations",
    tags=["admin-conversations"],
    # 会话明细 + 审计视图：B1 起额外要求 audit:read（admin 角色自带，operator/viewer 类角色可细化）
    dependencies=[Depends(get_current_admin), Depends(require_scope("audit:read"))],
)


def _serialize_conversation(c: Conversation, message_count: int = 0) -> dict:
    return {
        "id": c.id,
        "user_id": c.user_id,
        "session_id": c.session_id,
        "title": c.title,
        "status": c.status,
        "message_count": message_count,
        "last_active_at": str(c.last_active_at) if c.last_active_at else None,
        "created_at": str(c.created_at) if c.created_at else None,
    }


@router.get("")
async def list_conversations(
    keyword: str | None = None,
    status: str | None = None,
    user_id: str | None = None,
    page: int = 1,
    page_size: int = 20,
    admin: AdminIdentity = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """会话列表：keyword 匹配标题/会话 ID。"""
    page = max(page, 1)
    page_size = min(max(page_size, 1), 100)

    q = select(Conversation).where(Conversation.tenant_id == admin.tenant_id)
    if keyword:
        like = f"%{keyword}%"
        q = q.where((Conversation.title.like(like)) | (Conversation.session_id.like(like)))
    if status:
        q = q.where(Conversation.status == status)
    if user_id:
        q = q.where(Conversation.user_id == user_id)

    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar() or 0
    rows = (await db.execute(
        q.order_by(Conversation.last_active_at.desc().nullslast(), Conversation.id.desc())
        .offset((page - 1) * page_size).limit(page_size)
    )).scalars().all()

    counts: dict[int, int] = {}
    ids = [c.id for c in rows]
    if ids:
        count_rows = (await db.execute(
            select(ChatMessage.conversation_id, func.count())
            .where(
                ChatMessage.conversation_id.in_(ids),
                ChatMessage.tenant_id == admin.tenant_id,
            )
            .group_by(ChatMessage.conversation_id)
        )).all()
        counts = {cid: n for cid, n in count_rows}

    return {
        "conversations": [_serialize_conversation(c, counts.get(c.id, 0)) for c in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/{conversation_id}/messages")
async def get_messages(
    conversation_id: int,
    limit: int = 200,
    admin: AdminIdentity = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """会话消息明细（审计视角：全量角色）。"""
    conv = await db.get(Conversation, conversation_id)
    if not conv or int(conv.tenant_id or 1) != int(admin.tenant_id):
        raise BizException(BizErrorCode.NOT_FOUND, "会话不存在", status_code=404)
    rows = (await db.execute(
        select(ChatMessage)
        .where(
            ChatMessage.conversation_id == conversation_id,
            ChatMessage.tenant_id == admin.tenant_id,
        )
        .order_by(ChatMessage.seq).limit(min(limit, 500))
    )).scalars().all()
    return {
        "conversation": _serialize_conversation(conv),
        "messages": [
            {
                "id": m.id,
                "seq": m.seq,
                "role": m.role,
                # 对外展示只用脱敏列：原文列仅限临床审计追溯，不经普通后台接口输出
                "content": m.content_redacted or m.content,
                "event_type": m.event_type,
                "turn_id": m.turn_id,
                "created_at": str(m.created_at) if m.created_at else None,
            }
            for m in rows
        ],
        "total": len(rows),
    }


class ConversationStatusUpdate(BaseModel):
    status: str  # active / closed


@router.patch("/{conversation_id}/status")
async def update_conversation_status(
    conversation_id: int,
    req: ConversationStatusUpdate,
    admin: AdminIdentity = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """关闭 / 恢复会话。"""
    if req.status not in {"active", "closed"}:
        raise BizException(BizErrorCode.PARAM_ERROR, "status 只能是 active/closed", status_code=400)
    conv = await db.get(Conversation, conversation_id)
    if not conv or int(conv.tenant_id or 1) != int(admin.tenant_id):
        raise BizException(BizErrorCode.NOT_FOUND, "会话不存在", status_code=404)
    conv.status = req.status
    await db.flush()
    return {"message": "已关闭" if req.status == "closed" else "已恢复", "conversation": _serialize_conversation(conv)}


@router.get("/feedback/list")
async def list_feedback(
    rating: int | None = None,
    page: int = 1,
    page_size: int = 20,
    admin: AdminIdentity = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """用户反馈收集：患者端对 AI 回答的 👍/👎 与评论。"""
    page = max(page, 1)
    page_size = min(max(page_size, 1), 100)
    q = select(KnowledgeFeedback).where(
        KnowledgeFeedback.tenant_id == admin.tenant_id,
    )
    if rating in (1, -1):
        q = q.where(KnowledgeFeedback.rating == rating)

    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar() or 0
    rows = (await db.execute(
        q.order_by(KnowledgeFeedback.id.desc()).offset((page - 1) * page_size).limit(page_size)
    )).scalars().all()
    return {
        "feedback": [
            {
                "id": f.id,
                "user_id": f.user_id,
                "question": f.question,
                "answer": f.answer_preview,
                "rating": f.rating,
                "comment": f.comment,
                "intent": f.intent,
                "created_at": str(f.created_at) if f.created_at else None,
            }
            for f in rows
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
    }
