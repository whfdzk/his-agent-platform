# src/agents/knowledge/graph_rag.py

from __future__ import annotations
import json
from loguru import logger
from langchain_core.messages import SystemMessage
from langchain_core.language_models import BaseChatModel
from neo4j import AsyncDriver

from src.core.config import get_settings
from src.core.limits import clamp_retrieval
from src.core.langfuse_tracing import current_trace
from src.knowledge.cache import cached, make_key
from src.knowledge.llm_call import UTIL_TIMEOUT, call_llm, call_llm_text
from src.knowledge.prompts import (
    ENTITY_EXTRACT_PROMPT, NL2CYPHER_PROMPT,
)

settings = get_settings()
MAX_CYPHER_RETRIES = 2

# 实体抽取 / NL2Cypher 是工具内小调用，8s 足够；图查询本身另有 Neo4j 侧超时
GRAPH_UTIL_TIMEOUT = 8.0


async def _extract_entities(question: str, llm: BaseChatModel) -> dict:
    prompt = ENTITY_EXTRACT_PROMPT.format(question=question)
    response = await call_llm(
        llm, [SystemMessage(content=prompt)],
        timeout=GRAPH_UTIL_TIMEOUT, label="图谱实体抽取",
    )
    if response is None:
        return {"diseases": [], "symptoms": [], "drugs": [], "departments": [], "checks": []}
    try:
        content = (response.content or "").strip()
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
    cypher = await call_llm_text(
        llm, [SystemMessage(content=prompt)],
        timeout=GRAPH_UTIL_TIMEOUT, label="图谱 NTLCypher 生成",
    )
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
    util_llm: BaseChatModel | None = None,
    cache_question: str | None = None,
) -> list[dict]:
    """GraphRAG 检索，返回原始图谱查询结果（不经过 LLM 生成）。

    实体提取与 Cypher 生成都走 util_llm（关思考、限输出），只有最终整合用 llm。
    结果按问题缓存：同一轮里 drug_agent 与 knowledge_agent 会重复查同一条图谱。
    """
    u = util_llm or llm

    async def _compute() -> list[dict]:
        return await _graph_raw(question, neo4j_driver, u)

    # 同 doc_rag：键用原始问题，改写变了也能命中
    key_question = cache_question or question
    key = make_key("graph", key_question)
    return await cached(key, _compute, label=f"graph:{key_question[:14]}")


async def _graph_raw(question: str, neo4j_driver: AsyncDriver, llm: BaseChatModel) -> list[dict]:
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

