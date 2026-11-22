# src/agents/memory_middleware.py
#
# 长期记忆注入中间件。
#
# 背景（有实测证据）：supervisor 的 system_prompt 里写了"调用专项助手前先 search_memory"，
# 但真实链路里模型从不执行——日志统计：保存长期记忆 2 次、搜索长期记忆 0 次。
# 结果是记忆能存进 Milvus，跨会话却用不上（问"想吃阿莫西林"不会提示青霉素过敏）。
#
# 所以改成代码强制注入：每次调用模型前，用当前用户问题检索长期记忆，
# 命中足够相关的条目就拼进本次调用的 system_message。
# 用 system_message 而不是往 messages 里插消息，是为了不污染对话历史和 SSE 回流。
#
# 相似度阈值 0.45 来自实测：相关问法 0.507/0.571，无关问法 0.326~0.362。

from __future__ import annotations

import dataclasses
from typing import Any, Callable

from langchain.agents.middleware import AgentMiddleware, ModelRequest
from langchain_core.messages import HumanMessage, SystemMessage
from loguru import logger

from src.core.memory_schema import KIND_PRIORITY, label_of, resolve_kind

# 低于这个分数认为与当前问题无关，不注入（避免模型硬把无关病史塞进回答）
MIN_SCORE = 0.45
# 最多注入几条，防止 system_message 被撑爆
TOP_K = 3
# 同一问题的检索结果缓存条数（一次用户提问会触发多次模型调用：意图路由 + 工具往返）
CACHE_MAX = 200

MEMORY_HEADER = """## 该用户的长期健康记忆（系统按当前问题自动检索）
"""

MEMORY_FOOTER = """
以上是历史对话中记录的信息，可能过时，也可能与本轮无关：只在确实影响本轮建议时才引用（例如用药禁忌、过敏史），不要为了提到它而偏离用户当前的问题，也不要重复询问用户已经说过的内容。"""


class LongTermMemoryMiddleware(AgentMiddleware):
    """在每次模型调用前注入与当前问题相关的长期记忆。"""

    name = "long_term_memory"

    def __init__(self, store: Any, *, min_score: float = MIN_SCORE, top_k: int = TOP_K):
        self._store = store
        self._min_score = min_score
        self._top_k = top_k
        self._cache: dict[str, str] = {}

    # ------------------------------------------------------------------ #
    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Any],
    ) -> Any:
        try:
            memory_text = await self._build_memory_text(request)
        except Exception as e:  # 记忆是增强能力，任何异常都不能打断对话
            logger.warning(f"长期记忆注入跳过（{type(e).__name__}: {e}）")
            memory_text = ""

        if not memory_text:
            return await handler(request)

        base = ""
        if request.system_message is not None and isinstance(request.system_message.content, str):
            base = request.system_message.content
        new_system = SystemMessage(content=f"{base}\n\n{memory_text}")
        return await handler(dataclasses.replace(request, system_message=new_system))

    # ------------------------------------------------------------------ #
    async def _build_memory_text(self, request: ModelRequest) -> str:
        if self._store is None:
            return ""
        ctx = getattr(request.runtime, "context", None)
        user_id = getattr(ctx, "user_id", None)
        authenticated = getattr(ctx, "authenticated", True)
        # 游客没有长期记忆，不检索也不注入
        if not user_id or not authenticated:
            return ""

        question = self._last_user_text(request)
        if not question:
            return ""

        cached = self._cache.get(question)
        if cached is not None:
            return cached

        text = await self._recall(str(user_id), question)
        if len(self._cache) >= CACHE_MAX:
            self._cache.clear()
        self._cache[question] = text
        return text

    @staticmethod
    def _last_user_text(request: ModelRequest) -> str:
        messages = (request.state or {}).get("messages") or []
        for m in reversed(messages):
            if isinstance(m, HumanMessage):
                return m.content if isinstance(m.content, str) else ""
        return ""

    async def _recall(self, user_id: str, question: str) -> str:
        return await build_memory_block(
            self._store, user_id, question,
            min_score=self._min_score, top_k=self._top_k,
        )


async def build_memory_block(
    store: Any,
    user_id: str,
    question: str,
    *,
    min_score: float = MIN_SCORE,
    top_k: int = TOP_K,
) -> str:
    """按问题检索该用户的长期记忆，按类别分组返回可直接拼进消息的文本块。

    既给 supervisor 的 middleware 用，也给 handoff 出去的 worker 用：
    子 agent 是独立 create_agent，拿不到 supervisor 的 middleare 注入，
    不显式附加记忆就会给出与用户病史无关的通用回答。
    """
    if store is None or not user_id or not question:
        return ""

    # 多取一些候选再按「类别优先级 + 相似度」筛：记忆变多以后，
    # 过敏史容易被更长但更相近的无关记忆挤出 top_k，而它恰恰是安全相关信息。
    hits = await store.asearch(("users", user_id, "memories"), query=question, limit=top_k * 2)

    candidates: list[tuple[int, float, str, str]] = []
    for h in hits:
        score = h.score or 0.0
        if score < min_score:
            continue
        v = h.value or {}
        content = (v.get("content") or "").strip()
        if not content:
            continue
        kind = resolve_kind(content, v.get("kind"))
        candidates.append(
            (KIND_PRIORITY.get(kind, KIND_PRIORITY["other"]), -score, kind, content)
        )
    candidates.sort()

    seen: set[str] = set()
    picked: list[tuple[str, str]] = []
    for _pri, _neg_score, kind, content in candidates:
        if content in seen:  # 同一条内容重复存过时只注入一次
            continue
        seen.add(content)
        picked.append((kind, content))
        if len(picked) >= top_k:
            break

    if not picked:
        return ""

    groups: dict[str, list[str]] = {}
    for kind, content in picked:
        groups.setdefault(label_of(kind), []).append(content)
    blocks = [
        f"【{label}】\n" + "\n".join(f"- {c}" for c in contents)
        for label, contents in groups.items()
    ]

    logger.info(f"长期记忆注入 {len(picked)} 条（相似度 ≥ {min_score}，类别 {list(groups)}）")
    return MEMORY_HEADER + "\n".join(blocks) + "\n" + MEMORY_FOOTER
