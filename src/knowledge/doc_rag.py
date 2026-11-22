# src/agents/knowledge/doc_rag.py

from __future__ import annotations
import asyncio
import time

from loguru import logger
from langchain_core.messages import SystemMessage
from langchain_core.language_models import BaseChatModel
from langchain_core.embeddings import Embeddings
from pymilvus import MilvusClient

from src.core.concurrency import to_thread
from src.core.config import get_settings
from src.core.limits import clamp_retrieval
from src.core.metrics import observe_rag
from src.core.tracing import current_trace
from src.knowledge.prompts import DOC_QA_PROMPT

COLLECTION_NAME = "knowledge_docs"
# 引用核查的时间预算：超时就先给用户答案，不为了核查再等一轮模型
GROUNDING_TIMEOUT = 15.0
settings = get_settings()

# doc_type 过滤的兜底阈值：过滤后精排 top1 仍低于该分数，说明问题不适合按文档类型限定
# （例如问"ACEI 有什么禁忌"，模型传了 drug_instruction，而禁忌写在指南里），
# 自动回退全库检索一次并取更优结果。实测：过滤后 top1=0.513 / 全库 top1=0.754。
FILTER_FALLBACK_MIN_SCORE = 0.55


def _top1_score(hits: list[dict]) -> float:
    """精排结果首条分数（rerank 分数缺省时退回向量/融合分）。"""
    if not hits:
        return 0.0
    return float(hits[0].get("rerank_score") or hits[0].get("score") or 0.0)


def _score_of(hit: dict) -> tuple[float | None, str]:
    """返回 (分数, 分数语义)。

    只取 score 不标语义会误读：RRF 融合出现过多路命中，分数是排名累加分（实测 30.8），
    和余弦相似度（0~1）放一起看会被当成"极相关"。这里按来源逐档判断：
    rerank_score 存在 → 精排分；多路融合 → rrf_score；单路 → 该路原始分。
    """
    if hit.get("rerank_score") is not None:
        return hit["rerank_score"], "rerank"
    retrievers = hit.get("retrievers") or []
    if len(retrievers) > 1:
        score = hit.get("rrf_score")
        if score is None:
            score = hit.get("score")
        return score, "rrf"
    source = retrievers[0] if retrievers else (hit.get("retriever") or "vector")
    return hit.get("score"), ("bm25" if source == "bm25" else "vector")


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

    async def _search_once(type_filter: str | None) -> list[dict]:
        """跑一次完整检索：向量 + BM25 + RRF 融合 + 精排。"""
        t0 = time.perf_counter()
        if use_hyde and llm is not None:
            from src.knowledge.hyde import generate_hyde_embedding
            # 生成假设文档向量
            query_vec = await generate_hyde_embedding(question, llm, embedding_model)
        else:
            # 直接向量化查询
            query_vec = await embedding_model.aembed_query(question)

        search_params = {"metric_type": "COSINE", "params": {"nprobe": settings.MILVUS_NPROBE}}
        filter_expr = f'doc_type == "{type_filter}"' if type_filter else None

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
            {**hit["entity"], "score": hit.get("distance", 0.0), "retriever": "vector"}
            for hit in results[0]
        ]

        # 关键词召回（BM25）：向量对"ACEI""eGFR""收缩压 160"这类专有名词/缩写/数字不敏感，
        # 用 BM25 补这一路，再按排名做 RRF 融合（两路量纲不同，不能直接比分数）。
        # BM25 不可用（未装依赖 / 索引过大）时自动退回纯向量结果。
        from src.knowledge.bm25_store import bm25_search
        from src.knowledge.retrieval import rrf_fuse

        bm25_hits = await bm25_search(
            question, milvus_client, top_k=top_k, doc_type=type_filter,
        )
        if bm25_hits:
            hits = rrf_fuse([hits, bm25_hits], weights=[1.0, settings.BM25_WEIGHT])
            logger.info(
                f"检索融合: 向量 {len(results[0])} 条 + BM25 {len(bm25_hits)} 条 → {len(hits)} 条"
                f"（doc_type={type_filter or '全部'}）"
            )

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
                        "doc_type": hit.get("doc_type"),
                        "page_number": hit.get("page_number"),
                        "chunk_index": hit.get("chunk_index"),
                        "score": _score_of(hit)[0],
                        "score_type": _score_of(hit)[1],
                        # 摘句只用于 citation 定位，add_retrieval 内部会截断 + 过 PHI 正则
                        "text": hit.get("text"),
                    }
                    for hit in reranked
                ],
                top_k=rerank_top_k,
            )
        observe_rag("milvus", time.perf_counter() - t0, len(reranked))
        return reranked

    hits = await _search_once(doc_type)

    # 按文档类型过滤时，候选池天然被收窄：如果问题真正的答案在别的类型里
    # （典型的"某类药的禁忌"——答案在指南，不在说明书），精排分会明显偏低。
    # 这时回退一次全库检索，只有分数更高才采用，绝不为了兜底把结果变差。
    if doc_type:
        filtered_top1 = _top1_score(hits)
        if not hits or filtered_top1 < FILTER_FALLBACK_MIN_SCORE:
            fallback = await _search_once(None)
            fallback_top1 = _top1_score(fallback)
            if not hits or fallback_top1 > filtered_top1:
                logger.info(
                    f"doc_type={doc_type} 过滤召回不足（top1={filtered_top1:.3f}），"
                    f"已回退全库检索（top1={fallback_top1:.3f}，{len(fallback)} 条）"
                )
                return fallback

    return hits

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
    trace = current_trace()
    if trace is not None:
        trace.grounded = bool(check.get("is_grounded"))
    if check.get("is_grounded"):
        return answer

    claims = [str(c).strip() for c in (check.get("unsupported_claims") or []) if str(c).strip()]
    note = "【依据核查】知识库对该问题的覆盖有限，以下内容仅部分有文档支撑"
    if claims:
        note += "：" + "；".join(claims[:3])
    return f"{answer}\n\n{note}"