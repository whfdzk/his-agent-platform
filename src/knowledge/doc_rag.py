# src/agents/knowledge/doc_rag.py

from __future__ import annotations
import asyncio

from loguru import logger
from langchain_core.messages import SystemMessage
from langchain_core.language_models import BaseChatModel
from langchain_core.embeddings import Embeddings
from pymilvus import MilvusClient

from src.core.concurrency import to_thread
from src.core.config import get_settings
from src.core.limits import clamp_retrieval
from src.core.tracing import current_trace
from src.knowledge.prompts import DOC_QA_PROMPT

COLLECTION_NAME = "knowledge_docs"
# 引用核查的时间预算：超时就先给用户答案，不为了核查再等一轮模型
GROUNDING_TIMEOUT = 15.0
settings = get_settings()

# 只是搜索。
async def search_docs_raw(
    question: str,
    embedding_model: Embeddings,
    milvus_client: MilvusClient,
    top_k: int | None = None,
    rerank_top_k: int | None = None,
    doc_type: str | None = None,
    llm: BaseChatModel | None = None,
    use_hyde: bool = False,
) -> list[dict]:
    """
    文档向量检索，返回原始检索结果列表。
    支持 HyDE 增强检索和 Reranker 精排。
    """
    # 缺省值统一走 Settings，调参无需改代码
    top_k = top_k if top_k is not None else settings.RETRIEVAL_TOP_K
    rerank_top_k = rerank_top_k if rerank_top_k is not None else settings.RERANK_TOP_K
    # 单轮检索预算：返回条数不能超过配置上限（防超长上下文 + 防刷）
    rerank_top_k = clamp_retrieval(rerank_top_k)

    if use_hyde and llm is not None:
        from src.knowledge.hyde import generate_hyde_embedding
        # 生成假设文档向量
        query_vec = await generate_hyde_embedding(question, llm, embedding_model)
    else:
        # 直接向量化查询
        query_vec = await embedding_model.aembed_query(question)

    search_params = {"metric_type": "COSINE", "params": {"nprobe": settings.MILVUS_NPROBE}}
    filter_expr = f'doc_type == "{doc_type}"' if doc_type else None

    try:
        results = await to_thread(
            milvus_client.search,
            collection_name=COLLECTION_NAME,
            data=[query_vec],
            limit=top_k,
            output_fields=["doc_name", "doc_type", "page_number", "chunk_index", "text"],
            search_params=search_params,
            filter=filter_expr,
        )
    except Exception as e:
        logger.warning(f"文档检索失败: {e}")
        return []

    if not results or not results[0]:
        return []

    hits = [
        {**hit["entity"], "score": hit.get("distance", 0.0)}
        for hit in results[0]
    ]

    # 检索完后精排
    from src.knowledge.reranker import rerank_docs
    reranked = await rerank_docs(question, hits, top_k=rerank_top_k)

    # 全链路追踪：只记录来源元信息与分数，不记录正文
    trace = current_trace()
    if trace is not None:
        trace.add_retrieval(
            "milvus",
            [
                {
                    "doc_id": hit.get("chunk_index") or hit.get("id"),
                    "doc_name": hit.get("doc_name"),
                    "score": hit.get("score"),
                }
                for hit in reranked
            ],
            top_k=rerank_top_k,
        )
    return reranked

# 增强生成回答
# 追加溯源数据； 必须离线阶段构建milvus的collection的时候，collection schema有元数据
# 表结构定义的时候，必须多加几个存储文档元数据的列
def format_doc_context(hits: list[dict]) -> str:
    """将检索结果格式化为 LLM 可读的上下文字符串。
       统一格式: 片段1 [文档名, 第X页]: 内容... 。
    """
    if not hits:
        return ""
    parts = []
    for i, hit in enumerate(hits, 1):
        source = f"[{hit['doc_name']}, 第{hit.get('page_number', '?')}页]"
        parts.append(f"片段{i} {source}:\n{hit['text']}")
    return "\n\n---\n\n".join(parts)

# 搜索完成后会有 溯源数据 + 精排结果
async def search_docs(
    question: str,
    embedding_model: Embeddings,
    milvus_client: MilvusClient,
    llm: BaseChatModel,
    top_k: int | None = None,
    rerank_top_k: int | None = None,
    doc_type: str | None = None,
    role: str = "patient",
    use_hyde: bool = True,
    with_grounding: bool = False,
) -> str:
    """文档 RAG 检索 + Reranker 精排 + HyDE 增强 + 生成回答。

    with_grounding=True 时额外做一次引用核查（hallucination_check）：
    回答里缺少文档支撑的内容会被标注，避免"看起来像指南、其实是模型编的"。
    """
    hits = await search_docs_raw(
        question, embedding_model, milvus_client,
        top_k=top_k, rerank_top_k=rerank_top_k, doc_type=doc_type,
        llm=llm, use_hyde=use_hyde,
    )
    if not hits:
        return "当前知识库中未找到与您问题相关的文档内容。"

    context = format_doc_context(hits)

    prompt = DOC_QA_PROMPT.format(question=question, context=context, role=role)
    response = await llm.ainvoke([SystemMessage(content=prompt)])
    answer = response.content

    if not with_grounding:
        return answer

    from src.knowledge.hallucination_check import check_hallucination

    try:
        # 核查本身也是一次 LLM 调用：给个上限，超时就按"未核查"直接返回答案
        check = await asyncio.wait_for(
            check_hallucination(
                question=question, evidence=context, answer=answer, llm=llm,
            ),
            timeout=GROUNDING_TIMEOUT,
        )
    except Exception as e:
        logger.info(f"引用核查超时/失败（{type(e).__name__}），直接返回文档回答")
        return answer
    if check.get("is_grounded"):
        return answer

    claims = [str(c).strip() for c in (check.get("unsupported_claims") or []) if str(c).strip()]
    note = "【依据核查】知识库对该问题的覆盖有限，以下内容仅部分有文档支撑"
    if claims:
        note += "：" + "；".join(claims[:3])
    return f"{answer}\n\n{note}"