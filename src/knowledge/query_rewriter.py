# src/agents/knowledge/query_rewriter.py

from __future__ import annotations
import json
from loguru import logger
from langchain_core.messages import SystemMessage
from langchain_core.language_models import BaseChatModel

from src.knowledge.llm_call import UTIL_TIMEOUT, call_llm
from src.knowledge.prompts import QUERY_REWRITE_PROMPT


async def rewrite_query(
    question: str,
    llm: BaseChatModel,
    role: str = "patient",
) -> dict:
    """
    Query 改写：口语 → 医学术语，复杂问题拆分为子查询。
    返回 {"queries": [...], "intent": "..."}

    """
    prompt = QUERY_REWRITE_PROMPT.format(question=question, role=role)
    response = await call_llm(
        llm, [SystemMessage(content=prompt)],
        timeout=UTIL_TIMEOUT, label="查询改写",
    )
    if response is None:
        # 超时即退回原问题：多一次改写的收益不值得让用户等
        return {"queries": [question], "intent": "knowledge_qa"}
    try:
        content = (response.content or "").strip()
        if "```" in content:
            content = content.split("```")[1].lstrip("json").strip()
        result = json.loads(content)
        if "queries" not in result or not result["queries"]:
            result["queries"] = [question]
        if "intent" not in result:
            result["intent"] = "knowledge_qa"
        return result
    except Exception as e:
        logger.warning(f"Query 改写失败: {e}")
        return {"queries": [question], "intent": "knowledge_qa"}