# src/modules/chat/seq_allocator.py
#
# seq 分配器的"带数据库回填"版本：把 core/seq.py 的纯逻辑和一个 DB loader 组装起来。
#
# 为什么拆成两层：core 是最底层，不能 `from src.infra / src.modules`（test_layering
# 会直接失败）。所以 core/seq.py 只留"缓存优先 + 可注入回填"的逻辑，知道表模型的那部分
# 落在 modules 层。调用方（chat 路由、坐席回复）统一从这里拿，不再各自拼 loader。

from sqlalchemy import func, select

from src.core.seq import ensure_seq_base as _ensure_seq_base
from src.core.seq import next_seq as _next_seq


async def load_max_seq(user_id: str, session_id: str) -> int:
    """从 chat_messages 里取该会话已落库的最大 seq，用于计数器丢失后的回填。

    查不到会话时返回 0：意味着这是新会话，从 1 开始分配即可。
    """
    from src.infra.database import AsyncSessionLocal
    from src.modules.chat.model import ChatMessage, Conversation

    async with AsyncSessionLocal() as db:
        row = await db.execute(
            select(func.max(ChatMessage.seq))
            .select_from(ChatMessage)
            .join(Conversation, Conversation.id == ChatMessage.conversation_id)
            .where(
                Conversation.user_id == user_id,
                Conversation.session_id == session_id,
            )
        )
        return int(row.scalar() or 0)


async def ensure_seq_base(redis, user_id: str, session_id: str) -> int:
    return await _ensure_seq_base(redis, user_id, session_id, load_base=load_max_seq)


async def next_seq(redis, user_id: str, session_id: str) -> int:
    return await _next_seq(redis, user_id, session_id, load_base=load_max_seq)
