# src/agents/knowledge/doc_rag.py

from __future__ import annotations
import asyncio
import time

from loguru import logger
from langchain_core.language_models import BaseChatModel
from langchain_core.embeddings import Embeddings
from pymilvus import MilvusClient

from src.core.concurrency import to_thread
from src.core.config import get_settings
from src.core.limits import clamp_retrieval
from src.core.metrics import observe_rag
from src.core.langfuse_tracing import current_trace
from src.knowledge.cache import cached, make_key

COLLECTION_NAME = "knowledge_docs"
settings = get_settings()

# doc_type 过滤的兜底阈值：过滤后精排 top1 仍低于该分数，说明问题不适合按文档类型限定
# （例如问"ACEI 有什么禁忌"，模型传了 drug_instruction，而禁忌写在指南里），
# 自动回退全库检索一次并取更优结果。实测：过滤后 top1=0.513 / 全库 top1=0.754。
FILTER_FALLBACK_MIN_SCORE = 0.55

# 低权威来源配额：disease_kg 是第三方疾病百科（每个疾病 1 个 chunk，8000+ 条），
# 覆盖面广所以精排很容易占满 top_k，但内容陈旧且与指南冲突——实测 kb-01
# "高血压诊断标准"的 top5 里 3 条是疾病百科旧口径（收缩压 ≤140 正常、≥160 才算高），
# 和库里的 2024 指南 chunk 互相打架。做法是给低权威来源一个条数上限，
# 让指南/说明书这类有版本号的来源优先占位，不退化为"多召回几条百科"。
LOW_AUTHORITY_DOC_TYPES = {"disease_kg"}
LOW_AUTHORITY_MAX_RATIO = 0.3  # 最多占精排结果的 30%（top5 → 1 条）

# 指南相邻 chunk 扩展：同一节被切块切开时（危险分层表、联合用药表），
# 只召回半节会让回答缺关键定义。整轮最多补 2 条，避免上下文膨胀。
ADJACENT_EXPAND_MAX = 2


def _limit_low_authority(hits: list[dict], top_k: int) -> list[dict]:
    """压住低权威来源的条数，其余保持原精排顺序。"""
    quota = max(1, int(top_k * LOW_AUTHORITY_MAX_RATIO))
    kept: list[dict] = []
    low_used = 0
    for hit in hits:
        if (hit.get("doc_type") or "") in LOW_AUTHORITY_DOC_TYPES:
            if low_used >= quota:
                continue
            low_used += 1
        kept.append(hit)
    if len(kept) < len(hits):
        logger.info(
            f"来源配额：低权威条目 {len(hits) - len(kept)} 条被截掉"
            f"（{len(hits)} → {len(kept)}，配额 {quota}）"
        )
    return kept


def _expand_adjacent_chunks(hits: list[dict], milvus_client: MilvusClient) -> list[dict]:
    """给命中的指南补上相邻 chunk（±1）。

    为什么只做 guideline：分块会把同一节切在两块上——实测高血压指南
    chunk0 停在"中危：血压 1 级伴 1-2 个危险因素，或 2 级无危…"，
    "很高危：任何级别血压伴临床合并症（冠心病、脑卒中、心力衰竭、慢性肾病）"
    被切到 chunk1。只命中 chunk0 时，回答就只能凭模型常识，漏掉指南原话。
    指南一个文档只有 5~6 个 chunk，补相邻成本极低；疾病百科每条本身就是完整条目，不需要补。
    """
    seen = {(h.get("doc_name"), h.get("chunk_index")) for h in hits}
    out: list[dict] = []
    added = 0
    for hit in hits:
        out.append(hit)
        if added >= ADJACENT_EXPAND_MAX:
            continue
        if (hit.get("doc_type") or "") != "guideline":
            continue
        doc, idx = hit.get("doc_name"), hit.get("chunk_index")
        if not doc or idx is None:
            continue
        for nb in (int(idx) - 1, int(idx) + 1):
            if added >= ADJACENT_EXPAND_MAX:
                break
            if nb < 0 or (doc, nb) in seen:
                continue
            try:
                rows = milvus_client.query(
                    collection_name=COLLECTION_NAME,
                    filter=f'doc_name == "{doc}" and chunk_index == {nb}',
                    output_fields=["doc_name", "doc_type", "page_number", "chunk_index", "text"],
                    limit=1,
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(f"相邻 chunk 查询失败 {doc}#{nb}: {type(e).__name__}: {e}")
                continue
            if not rows:
                continue
            seen.add((doc, nb))
            row = dict(rows[0])
            row["score"] = float(hit.get("score") or 0.0) * 0.95
            row["retriever"] = "adjacent"
            # 紧跟原命中插入：format_doc_context 会按顺序截断到 DOC_CONTEXT_MAX_CHARS，
            # 放列表末尾容易被截掉，等于白补
            out.append(row)
            added += 1
            logger.info(f"相邻 chunk 扩展：{doc}#{nb} 并入证据（原命中 #{idx}）")
    return out


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
    cache_question: str | None = None,
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

        async def _vector_search() -> list[dict]:
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
            return [
                {**hit["entity"], "score": hit.get("distance", 0.0), "retriever": "vector"}
                for hit in results[0]
            ]

        # 关键词召回（BM25）：向量对"ACEI""eGFR""收缩压 160"这类专有名词/缩写/数字不敏感，
        # 用 BM25 补这一路，再按排名做 RRF 融合（两路量纲不同，不能直接比分数）。
        # 两路互不依赖，并发跑：BM25 首次要建 8k+ chunk 索引，串行会让向量检索白等。
        from src.knowledge.bm25_store import bm25_search
        from src.knowledge.retrieval import rrf_fuse

        vec_hits, bm25_hits = await asyncio.gather(
            _vector_search(),
            bm25_search(question, milvus_client, top_k=top_k, doc_type=type_filter),
        )
        hits = vec_hits
        if bm25_hits:
            hits = rrf_fuse([vec_hits, bm25_hits], weights=[1.0, settings.BM25_WEIGHT])
            logger.info(
                f"检索融合: 向量 {len(vec_hits)} 条 + BM25 {len(bm25_hits)} 条 → {len(hits)} 条"
                f"（doc_type={type_filter or '全部'}）"
            )

        # 检索完后精排
        from src.knowledge.reranker import rerank_docs
        reranked = await rerank_docs(question, hits, top_k=rerank_top_k)
        reranked = _limit_low_authority(reranked, rerank_top_k)
        # 指南被切块切开时补相邻 chunk：失败不影响主检索，只丢这层增强
        if reranked:
            try:
                reranked = await to_thread(_expand_adjacent_chunks, reranked, milvus_client)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"相邻 chunk 扩展失败（跳过）: {type(e).__name__}: {e}")

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

    async def _compute() -> list[dict]:
        """真实检索；结果按 (问题, doc_type, 参数) 缓存，同轮/跨轮重复问直接复用。"""
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

    # 缓存键用调用方的原始问题：改写是会变的（同一句话两次改写措辞可能不同），
    # 用改写后的问题做键会让"同一轮里又问一遍"命中不了。
    key_question = cache_question or question
    key = make_key("docs", key_question, doc_type, top_k, rerank_top_k)
    return await cached(key, _compute, label=f"docs:{key_question[:14]}")

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

