# 精排属于后检索阶段。检索完成以后，精排模型，根据检索结果，找到语义上更相关的。
# src/agents/knowledge/reranker.py

from __future__ import annotations
from loguru import logger

from src.core.config import get_settings

settings = get_settings()


async def rerank_docs(
    query: str,
    documents: list[dict],
    top_k: int = 3,
) -> list[dict]:
    """
    使用 DashScope Reranker 对检索结果精排。
    输入：query + 粗检索结果列表（每个元素含 "text" 字段）
    输出：按相关性重排后的 top_k 结果
    """
    if not documents:
        return []

    # 只有 1 条时没有可重排的余地。
    # 注意不能写成 len(documents) <= top_k：候选数本来就不多时（知识库小、或过滤后剩几条），
    # 这个条件恒成立，精排会被永久跳过——顺序也就永远停在粗检索的顺序上。
    if len(documents) <= 1:
        return documents[:top_k]

    try:
        import dashscope
        from dashscope import TextReRank

        dashscope.api_key = settings.DASHSCOPE_API_KEY
        # 提取文档文本
        texts = [doc.get("text", "") for doc in documents]

        # 精排
        response = TextReRank.call(
            model=settings.RERANK_MODEL,
            query=query,
            documents=texts, # 精排
            top_n=top_k,
            return_documents=False,
        )

        if response.status_code != 200:
            logger.warning(f"Reranker 调用失败: {response.message}")
            return documents[:top_k]

        reranked = []
        for item in response.output.results:
            idx = item.index
            doc = documents[idx].copy()
            doc["rerank_score"] = item.relevance_score
            reranked.append(doc)

        return reranked

    except ImportError:
        logger.warning("dashscope 未安装，回退到向量距离排序")
        return documents[:top_k]
    except Exception as e:
        logger.warning(f"Reranker 异常，回退到向量距离排序: {e}")
        return documents[:top_k]