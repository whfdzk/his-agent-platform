# src/agents/knowledge/graph_rag.py

from __future__ import annotations
import json
from loguru import logger
from langchain_core.messages import SystemMessage
from langchain_core.language_models import BaseChatModel
from neo4j import AsyncDriver

from src.core.config import get_settings
from src.core.limits import clamp_retrieval
from src.core.tracing import current_trace
from src.knowledge.prompts import (
    ENTITY_EXTRACT_PROMPT, NL2CYPHER_PROMPT, GRAPH_QA_PROMPT,
)

settings = get_settings()
MAX_CYPHER_RETRIES = 2


async def _extract_entities(question: str, llm: BaseChatModel) -> dict:
    prompt = ENTITY_EXTRACT_PROMPT.format(question=question)
    response = await llm.ainvoke([SystemMessage(content=prompt)])
    try:
        content = response.content.strip()
        if "```" in content:
            content = content.split("```")[1].lstrip("json").strip()
        return json.loads(content)
    except Exception as e:
        logger.warning(f"实体提取失败: {e}")
        return {"diseases": [], "symptoms": [], "drugs": [], "departments": [], "checks": []}


async def _generate_cypher(
    question: str, entities: dict, llm: BaseChatModel, error_hint: str = "",
) -> str:
    extra = ""
    if error_hint:
        extra = f"\n\n上一次生成的 Cypher 执行报错：{error_hint}\n请修正后重新生成。"
    prompt = NL2CYPHER_PROMPT.format(
        question=question,
        entities=json.dumps(entities, ensure_ascii=False),
    ) + extra
    response = await llm.ainvoke([SystemMessage(content=prompt)])
    cypher = response.content.strip()
    if "```" in cypher:
        cypher = cypher.split("```")[1].lstrip("cypher").strip()
    return cypher


async def _execute_cypher(cypher: str, neo4j_driver: AsyncDriver) -> list[dict]:
    if not cypher:
        return []
    async with neo4j_driver.session() as session:
        result = await session.run(cypher)
        return await result.data()


# 根据用户问题，生成 Cypher 查询语句，执行图查询。返回原始查询结果。
async def search_graph_raw(
    question: str,
    neo4j_driver: AsyncDriver,
    llm: BaseChatModel,
) -> list[dict]:
    """GraphRAG 检索，返回原始图谱查询结果（不经过 LLM 生成）。"""
    entities = await _extract_entities(question, llm)
    # 补一层"类别词/口语症状 → 图谱实际存法"的映射，否则 ACEI、干咳这类词
    # 去 MATCH 节点名恒为 0（图谱存的是"缬沙坦胶囊""咳嗽"）
    from src.knowledge.entity_alias import normalize_entities

    entities = normalize_entities(entities)
    logger.info(f"GraphRAG 实体提取: {entities}")

    error_hint = ""
    for attempt in range(MAX_CYPHER_RETRIES + 1):
        cypher = await _generate_cypher(question, entities, llm, error_hint)
        logger.info(f"GraphRAG Cypher (attempt {attempt + 1}): {cypher}")
        try:
            records = await _execute_cypher(cypher, neo4j_driver)
            limit = min(settings.GRAPH_RAG_RECORD_LIMIT, clamp_retrieval(None))
            records = records[:limit]
            trace = current_trace()
            if trace is not None:
                trace.add_retrieval(
                    "neo4j",
                    [
                        {
                            "doc_id": str(rec.get("id") or rec.get("name") or ""),
                            "title": str(rec.get("name") or rec.get("disease") or ""),
                        }
                        for rec in records[:20]
                        if isinstance(rec, dict)
                    ],
                    top_k=limit,
                )
            return records
        except Exception as e:
            error_hint = str(e)
            logger.warning(f"Cypher 执行失败 (attempt {attempt + 1}): {e}")
            if attempt == MAX_CYPHER_RETRIES:
                return []
    return []


# 增强生成回答
async def search_graph(
    question: str,
    neo4j_driver: AsyncDriver,
    llm: BaseChatModel,
    role: str = "patient",
) -> str:
    """GraphRAG 完整流程：实体提取 → NL2Cypher（带重试）→ LLM 整合。"""
    records = await search_graph_raw(question, neo4j_driver, llm)

    if not records:
        return "知识图谱中未找到与您问题相关的信息。"

    graph_result = json.dumps(records, ensure_ascii=False, indent=2)
    prompt = GRAPH_QA_PROMPT.format(
        question=question, graph_result=graph_result, role=role,
    )
    response = await llm.ainvoke([SystemMessage(content=prompt)])
    return response.content