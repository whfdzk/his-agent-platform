# src/knowledge/bm25_store.py
#
# 关键词召回（BM25）：向量检索的补位通道。
#
# 为什么需要它：
# - 向量召回对"专有名词 / 缩写 / 数字"不敏感（如"ACEI""eGFR 45""收缩压 160"），
#   这类词恰恰是医学问答里最不能丢的部分。
# - BM25 按词频打分，正好补这块；两路用 RRF 融合，比单路稳。
#
# 实现取舍：
# - 数据量小（当前知识库只有个位数 chunk），直接对 Milvus 里的 chunk 建内存索引，
#   不引入额外的全文检索引擎；索引有 TTL，超时自动重建。
# - chunk 数超过 BM25_MAX_INDEX_DOCS 时不再建索引，直接退回纯向量检索，
#   避免把内存吃满。
# - 任何异常都返回空列表，检索链路不允许因为"补充通道"整体失败。

from __future__ import annotations

import asyncio
import logging
import time

from loguru import logger
from pymilvus import MilvusClient
from rank_bm25 import BM25Okapi

from src.core.concurrency import to_thread
from src.core.config import get_settings

settings = get_settings()

COLLECTION_NAME = "knowledge_docs"
OUTPUT_FIELDS = ["doc_name", "doc_type", "page_number", "chunk_index", "text"]

# 索引缓存：{built_at, docs, bm25}
_index: dict | None = None
_index_lock = asyncio.Lock()
_tokenizer_ready = False


def _tokenize(text: str) -> list[str]:
    """中文分词：jieba 搜索引擎模式，保留单字与英文术语。"""
    global _tokenizer_ready
    import jieba

    if not _tokenizer_ready:
        # 关掉 jieba 加载词典时的 INFO 日志，否则每次首次调用刷屏
        jieba.setLogLevel(logging.WARNING)
        _tokenizer_ready = True
    return [t.strip() for t in jieba.lcut_for_search(text or "") if t.strip()]


def _build_index(client: MilvusClient) -> dict:
    """从 Milvus 拉全量 chunk 建 BM25 索引（同步函数，由 to_thread 调用）。"""
    rows = client.query(
        collection_name=COLLECTION_NAME,
        filter="",
        output_fields=OUTPUT_FIELDS,
        limit=settings.BM25_MAX_INDEX_DOCS,
    )
    docs = [dict(r) for r in rows if str(r.get("text") or "").strip()]
    corpus = [_tokenize(d["text"]) for d in docs]
    bm25 = BM25Okapi(corpus) if corpus else None
    logger.info(f"BM25 索引构建完成: {len(docs)} 个 chunk")
    return {"built_at": time.time(), "docs": docs, "bm25": bm25}


async def _get_index(client: MilvusClient) -> dict | None:
    """取索引；过期或未建时重建。并发重建只跑一次。"""
    global _index

    now = time.time()
    if _index is not None and now - _index["built_at"] < settings.BM25_INDEX_TTL_SECONDS:
        return _index

    async with _index_lock:
        # 双重检查：等锁期间可能已被别的协程建好
        if _index is not None and time.time() - _index["built_at"] < settings.BM25_INDEX_TTL_SECONDS:
            return _index
        try:
            _index = await to_thread(_build_index, client)
        except Exception as e:
            logger.warning(f"BM25 索引构建失败，本次只用向量召回: {type(e).__name__}: {e}")
            return None
        return _index


def invalidate() -> None:
    """知识库有增删时调用，下次检索强制重建索引。"""
    global _index
    _index = None
    try:
        # 检索结果缓存也一起清：否则新旧 chunk 混着用，命中缓存时看不到新导入的内容
        from src.knowledge.cache import clear as clear_query_cache

        clear_query_cache()
    except Exception:
        pass


async def warmup() -> None:
    """启动时后台建一次索引。8755 个 chunk 约 9 秒，
    否则进程重启 / TTL 到期后的第一个知识查询要白等这 9 秒。"""
    try:
        from src.infra.milvus_client import get_milvus_client

        client = await to_thread(get_milvus_client)
        index = await _get_index(client)
        if index is not None:
            logger.info(f"BM25 索引预热完成: {len(index['docs'])} 个 chunk")
    except Exception as e:
        logger.warning(f"BM25 索引预热失败（首次查询将较慢）: {type(e).__name__}: {e}")


def _score_and_pick(index: dict, query: str, top_k: int, doc_type: str | None) -> list[dict]:
    """BM25 打分并取 top_k（同步函数，由 to_thread 调用）。"""
    bm25: BM25Okapi | None = index["bm25"]
    if bm25 is None:
        return []

    tokens = _tokenize(query)
    if not tokens:
        return []

    scores = bm25.get_scores(tokens)
    order = scores.argsort()[::-1]

    picked: list[dict] = []
    for idx in order:
        score = float(scores[int(idx)])
        if score <= 0:
            # 分数为 0 说明 query 词一个都没命中，后面的更不可能命中
            break
        doc = index["docs"][int(idx)]
        if doc_type and doc.get("doc_type") != doc_type:
            continue
        hit = {**doc, "score": score, "retriever": "bm25"}
        picked.append(hit)
        if len(picked) >= top_k:
            break
    return picked


async def bm25_search(
    query: str,
    client: MilvusClient | None,
    top_k: int | None = None,
    doc_type: str | None = None,
) -> list[dict]:
    """关键词召回：返回 [{...chunk 字段, score, retriever: "bm25"}]。

    任何异常都返回 []，调用方按"这一路没结果"处理。
    """
    if not settings.ENABLE_BM25 or client is None or not query.strip():
        return []

    top_k = top_k or settings.BM25_TOP_K
    index = await _get_index(client)
    if index is None:
        return []

    try:
        return await to_thread(_score_and_pick, index, query, top_k, doc_type)
    except Exception as e:
        logger.warning(f"BM25 检索失败: {type(e).__name__}: {e}")
        return []
