# src/knowledge/fusion.py
#
# 多通道检索：文档库 + 知识图谱并行召回，把两路证据拼接成一段可直接阅读的原文，
# 交给调用方（agent）去写答案。
#
# 为什么这里不再"融合生成回答"（实测数据）：
#   原实现对 46 条文档片段 + 图谱记录拼出一个 3588 字的提示词，让主模型写答案，
#   单次生成 38.9s / 2300 字，历史峰值 2857 token / 35s；紧跟一次幻觉核查又要 8s。
#   而调用它的 agent 拿到这段答案后还会再写一遍自己的回答——同一份内容生成两遍。
#   对"高血压平时要注意什么"实测：单独召回 12.7s，融合生成把整轮拖到 60~140s
#   （r11-24152 直接撞 150s 超时降级）。
#   现在只回证据：耗时降到召回本身（~13s），答案由 agent 一次写成。

from __future__ import annotations

import asyncio
import json

from langchain_core.embeddings import Embeddings
from loguru import logger
from neo4j import AsyncDriver
from pymilvus import MilvusClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import get_settings
from src.knowledge.doc_rag import format_doc_context, search_docs_raw
from src.knowledge.graph_rag import search_graph_raw

settings = get_settings()

# 单通道最多回给模型的证据长度：够支撑答案，又不至于把上下文顶爆
CHANNEL_SNIPPET_LIMIT = 2200


async def multi_channel_search(
    question: str,
    llm,
    embedding_model: Embeddings,
    milvus_client: MilvusClient,
    neo4j_driver: AsyncDriver,
    db_session: AsyncSession | None = None,
    channels: list[str] | None = None,
    role: str = "patient",
    util_llm=None,
    cache_question: str | None = None,
) -> str:
    """多通道并行检索，返回拼接后的证据原文（不生成回答）。

    channels: 指定使用哪些通道 ["doc_rag", "graph_rag", "nl2sql"]，默认 doc_rag + graph_rag
    """
    if channels is None:
        channels = ["doc_rag", "graph_rag"]

    tasks = {}
    if "doc_rag" in channels:
        tasks["doc_rag"] = search_docs_raw(
            question, embedding_model, milvus_client,
            cache_question=cache_question,
        )
    if "graph_rag" in channels:
        tasks["graph_rag"] = search_graph_raw(
            question, neo4j_driver, util_llm or llm, util_llm=util_llm,
            cache_question=cache_question,
        )
    if "nl2sql" in channels and db_session:
        from src.knowledge.nl2sql import search_sql

        tasks["nl2sql"] = search_sql(question, util_llm or llm, db_session)

    results = {}
    gathered = await asyncio.gather(*tasks.values(), return_exceptions=True)
    for key, result in zip(tasks.keys(), gathered):
        if isinstance(result, Exception):
            logger.warning(f"通道 {key} 检索失败: {result}")
            results[key] = None
        else:
            results[key] = result

    parts: list[str] = []

    doc_hits = results.get("doc_rag")
    if doc_hits:
        ctx = format_doc_context(doc_hits)[:CHANNEL_SNIPPET_LIMIT]
        parts.append(f"### 文档检索结果\n{ctx}")

    graph_records = results.get("graph_rag")
    if graph_records:
        graph_str = json.dumps(graph_records, ensure_ascii=False)
        parts.append(f"### 知识图谱检索结果\n{graph_str[:CHANNEL_SNIPPET_LIMIT]}")

    sql_answer = results.get("nl2sql")
    if sql_answer and isinstance(sql_answer, str):
        parts.append(f"### 运营数据查询结果\n{sql_answer[:CHANNEL_SNIPPET_LIMIT]}")

    if not parts:
        return "所有检索通道均未找到与您问题相关的信息。"
    return "\n\n".join(parts)
