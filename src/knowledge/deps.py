# src/knowledge/deps.py
#
# 知识链路（文档 RAG / 图谱 RAG / 多通道融合 / 处方审核 / NL2SQL）的依赖装配。
#
# 为什么用 contextvar 而不是把 deps 塞进工具闭包：
#   工具集在 Agent 创建时构建一次（create_agent 编译图不便宜），
#   而 llm/向量库/图数据库/DB 会话是"每次请求一套"。用 contextvar 绑定，
#   Agent 可以继续做单例，工具在真正被调用时取当前请求的依赖。
#
# 依赖方向：knowledge.deps ← knowledge.tools ← agents.workers ← agents.worker_tools

from __future__ import annotations

import asyncio
import time
from contextlib import contextmanager
from contextvars import ContextVar
from collections.abc import Iterator

from langchain_core.embeddings import Embeddings
from langchain_core.language_models import BaseChatModel
from loguru import logger
from neo4j import AsyncDriver
from pymilvus import MilvusClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import get_llm, get_settings
from src.core.permissions import is_staff_role
from src.infra.embedding import get_embeddings
from src.infra.milvus_client import get_milvus_client
from src.infra.neo4j_client import get_neo4j_driver

settings = get_settings()

# 工具型调用的输出上限：抽取/改写/打分/核查只需要几十个 token，
# 放开上限时模型会把中间结果写成小作文（实测有 1,300 token 的打分输出）
UTIL_MAX_TOKENS = 512


class KnowledgeDeps:
    """知识 Agent 的依赖注入容器（一次请求一套）。"""

    def __init__(
        self,
        llm: BaseChatModel,
        embedding_model: Embeddings | None,
        milvus_client: MilvusClient | None,
        neo4j_driver: AsyncDriver | None,
        db_session: AsyncSession | None = None,
        user_id: str = "anonymous",
        role: str = "patient",
        util_llm: BaseChatModel | None = None,
    ):
        self.llm = llm
        # 工具型调用专用模型：关掉思考模式、限制输出长度。
        # 最终回答仍用 self.llm（保留推理能力）。
        self.util_llm = util_llm or llm
        self.embedding_model = embedding_model
        self.milvus_client = milvus_client
        self.neo4j_driver = neo4j_driver
        self.db_session = db_session
        self.user_id = user_id
        self.role = role

    @property
    def is_staff(self) -> bool:
        """院内角色才允许走结构化数据（NL2SQL）。

        口径与 core.permissions.STAFF_ROLES 保持一致：
        以前这里手写了 {"doctor", "pharmacist", "staff", "admin"}，
        登录签发的 clinician/nurse 反而被判成患者，运营查询永远拿不到数据。
        """
        return is_staff_role(self.role)


_current_deps: ContextVar[KnowledgeDeps | None] = ContextVar("knowledge_deps", default=None)


def current_knowledge_deps() -> KnowledgeDeps | None:
    return _current_deps.get()


@contextmanager
def bind_knowledge_deps(deps: KnowledgeDeps | None) -> Iterator[KnowledgeDeps | None]:
    """在当前任务上下文里绑定知识依赖，供工具在调用时取用。"""
    token = _current_deps.set(deps)
    try:
        yield deps
    finally:
        _current_deps.reset(token)


# Milvus 不可用时退避（与 supervisor 长期记忆同一套策略）：
# 知识检索是增强能力，不该让一次连接超时拖垮整轮对话
_RETRY_INTERVAL = 60.0
_milvus_retry_at = 0.0


async def _get_milvus_client() -> MilvusClient | None:
    """知识库检索要用 MilvusClient 高层 API，不能拿 ORM 的 alias 混用。"""
    global _milvus_retry_at
    if time.monotonic() < _milvus_retry_at:
        return None
    try:
        client = await asyncio.to_thread(get_milvus_client)
        _milvus_retry_at = 0.0
        return client
    except Exception as e:
        _milvus_retry_at = time.monotonic() + _RETRY_INTERVAL
        logger.warning(f"知识库 Milvus 不可用，文档检索降级（{_RETRY_INTERVAL:.0f}s 后重试）: {e}")
        return None


def _get_neo4j() -> AsyncDriver | None:
    try:
        return get_neo4j_driver()
    except Exception as e:
        logger.warning(f"知识图谱 Neo4j 不可用，图谱检索降级: {e}")
        return None


async def build_knowledge_deps(
    db_session: AsyncSession | None = None,
    user_id: str = "anonymous",
    role: str = "patient",
    temperature: float = 0.2,
) -> KnowledgeDeps:
    """装配一套知识依赖；基础设施不可用时对应组件为 None（工具内部降级）。"""
    try:
        embedding_model: Embeddings | None = get_embeddings()
    except Exception as e:
        logger.warning(f"Embedding 不可用，向量检索降级: {e}")
        embedding_model = None

    return KnowledgeDeps(
        llm=get_llm(temperature=temperature),
        util_llm=get_llm(
            temperature=0.0, thinking=False, max_tokens=UTIL_MAX_TOKENS,
        ),
        embedding_model=embedding_model,
        milvus_client=await _get_milvus_client(),
        neo4j_driver=_get_neo4j(),
        db_session=db_session,
        user_id=user_id,
        role=role,
    )
