# src/core/memory.py
#
# Agent 记忆模块：短期记忆 + 长期记忆。
#
# 设计：
# - 短期记忆：会话内对话历史，用 Redis 存（已有 checkpointer）
# - 长期记忆：用户的偏好、病史、过敏史等，用 PostgreSQL 存
# - 记忆自动提取：从对话中提取关键信息，写入长期记忆
# - 记忆检索：对话开始时，自动检索相关长期记忆注入上下文

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from loguru import logger


class MemoryType(str, Enum):
    """记忆类型。"""
    PREFERENCE = "preference"  # 用户偏好
    MEDICAL_HISTORY = "medical_history"  # 病史
    ALLERGY = "allergy"  # 过敏史
    CONTACT = "contact"  # 联系方式
    BEHAVIOR = "behavior"  # 行为模式
    OTHER = "other"  # 其他


@dataclass
class LongTermMemory:
    """长期记忆条目。"""
    id: int | None = None
    user_id: str = ""
    memory_type: MemoryType = MemoryType.OTHER
    content: str = ""  # 记忆内容
    importance: int = 5  # 重要性 1-10
    source: str = ""  # 来源：对话/手动录入/系统
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    last_used_at: datetime = field(default_factory=datetime.now)
    use_count: int = 0  # 被引用次数


class MemoryManager:
    """
    记忆管理器：负责短期记忆和长期记忆的读写。
    
    面试亮点：
    1. 分层记忆架构：短期（会话内）+ 长期（跨会话）
    2. 自动记忆提取：从对话中提取关键信息
    3. 记忆检索：按重要性和相关性排序
    4. 记忆衰减：长期未使用的记忆自动降权
    """
    
    def __init__(self):
        # 内存缓存：用户ID -> 记忆列表
        self._memory_cache: dict[str, list[LongTermMemory]] = {}
        # 短期记忆：会话ID -> 对话历史（这里用内存，实际用 Redis）
        self._short_term: dict[str, list[dict[str, Any]]] = {}
    
    async def add_memory(
        self,
        user_id: str,
        memory_type: MemoryType,
        content: str,
        importance: int = 5,
        source: str = "对话提取",
    ) -> LongTermMemory:
        """
        添加一条长期记忆。
        
        场景：从用户对话中提取到"我对青霉素过敏"，自动写入长期记忆。
        """
        memory = LongTermMemory(
            user_id=user_id,
            memory_type=memory_type,
            content=content,
            importance=importance,
            source=source,
        )
        
        # 缓存里加一条
        if user_id not in self._memory_cache:
            self._memory_cache[user_id] = []
        self._memory_cache[user_id].append(memory)
        
        logger.info(f"添加长期记忆: user={user_id} type={memory_type} content={content[:30]}")
        return memory
    
    async def get_relevant_memories(
        self,
        user_id: str,
        query: str,
        top_k: int = 5,
    ) -> list[LongTermMemory]:
        """
        检索与当前问题相关的长期记忆。
        
        场景：用户问"我感冒了能吃什么药"，自动检索：
        - 青霉素过敏 → 不能开青霉素类
        - 之前有高血压 → 注意药物相互作用
        """
        memories = self._memory_cache.get(user_id, [])
        if not memories:
            return []
        
        # 简单相关性评分：关键词匹配 + 重要性 + 最近使用
        scored = []
        for m in memories:
            score = 0
            # 关键词匹配（简化版，实际用 embedding）
            query_words = set(query.lower().split())
            memory_words = set(m.content.lower().split())
            overlap = len(query_words & memory_words)
            score += overlap * 10
            
            # 重要性权重
            score += m.importance
            
            # 最近使用权重（越近越重要）
            days_since_use = (datetime.now() - m.last_used_at).days
            score += max(0, 10 - days_since_use)
            
            scored.append((score, m))
        
        # 按分数排序，取 top_k
        scored.sort(key=lambda x: x[0], reverse=True)
        result = [m for _, m in scored[:top_k]]
        
        # 更新使用次数和时间
        for m in result:
            m.use_count += 1
            m.last_used_at = datetime.now()
        
        logger.debug(f"检索到 {len(result)} 条相关记忆: user={user_id} query={query[:20]}")
        return result
    
    async def add_to_short_term(
        self,
        session_id: str,
        role: str,
        content: str,
    ) -> None:
        """添加到短期记忆（会话历史）。"""
        if session_id not in self._short_term:
            self._short_term[session_id] = []
        
        self._short_term[session_id].append({
            "role": role,
            "content": content,
            "timestamp": datetime.now().isoformat(),
        })
        
        # 只保留最近 20 条，防止上下文爆炸
        if len(self._short_term[session_id]) > 20:
            self._short_term[session_id] = self._short_term[session_id][-20:]
    
    async def get_short_term_history(self, session_id: str) -> list[dict[str, Any]]:
        """获取短期记忆（对话历史）。"""
        return self._short_term.get(session_id, [])
    
    def format_memories_for_prompt(self, memories: list[LongTermMemory]) -> str:
        """把记忆格式化成 prompt 片段。"""
        if not memories:
            return ""
        
        lines = ["【用户长期记忆】"]
        for m in memories:
            lines.append(f"- [{m.memory_type.value}] {m.content}")
        return "\n".join(lines)


# 全局单例
_memory_manager: MemoryManager | None = None


def get_memory_manager() -> MemoryManager:
    """获取全局记忆管理器单例。"""
    global _memory_manager
    if _memory_manager is None:
        _memory_manager = MemoryManager()
    return _memory_manager
