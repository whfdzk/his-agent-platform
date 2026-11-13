# src/agents/inquiry/deps.py
#
# 问诊流程的依赖容器与工厂。
#
# 单独成文件，nodes.py 与 graph.py 才能只依赖类型与容器，
# 而不用各自 import 一堆 infra 客户端。

from __future__ import annotations

from src.core.config import get_llm
from src.infra.embedding import get_embeddings
from src.infra.milvus_client import aget_milvus_client_alias, get_milvus_client
from src.infra.neo4j_client import get_neo4j_driver

# ── 依赖注入容器（在 graph 编译时注入，避免全局单例） ──────────────────────
class InquiryDeps:
    def __init__(self, llm, neo4j_driver, embedding_model, milvus_client, db_session,
                 persist: bool = True):
        self.llm = llm
        self.neo4j_driver = neo4j_driver
        self.embedding_model = embedding_model
        self.milvus_client = milvus_client
        self.db_session = db_session
        # persist=False：游客模式 —— 不写问诊记录/预约等持久化数据，
        # 状态只存在 Redis（会话级短期记忆，1 小时过期）
        self.persist = persist


async def build_inquiry_deps(db_session=None, persist: bool = True) -> InquiryDeps:
    """
    构建问诊依赖注入容器的工厂函数。
    供 call_inquiry_agent 工具和 FastAPI 路由共用，避免重复代码。

    persist=False 表示游客模式：只做问答，不写问诊记录/预约等持久化数据。
    """
    llm = get_llm(temperature=0.3)
    neo4j_driver = get_neo4j_driver()
    # 确保 alias 连接已建立（长期记忆走 ORM Collection API）
    await aget_milvus_client_alias()
    return InquiryDeps(
        llm=llm,
        neo4j_driver=neo4j_driver,
        embedding_model=get_embeddings(),
        milvus_client=get_milvus_client(),
        db_session=db_session,
        persist=persist,
    )