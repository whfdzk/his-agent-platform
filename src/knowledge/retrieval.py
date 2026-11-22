# src/knowledge/retrieval.py
#
# 多路召回结果的融合工具：RRF（Reciprocal Rank Fusion）。
#
# 分工说明（避免误解这一层的职责）：
# - 真正的"多路 + LLM 融合回答"入口是 src/knowledge/fusion.py::multi_channel_search
#   （doc_rag / graph_rag / nl2sql 三路并行 → 融合 → 幻觉检测）。
# - 本模块只做一件事：把同一层里的多路召回（向量 / BM25）按排名融合成一份有序结果，
#   供 doc_rag 在精排前使用。
#
# 为什么用 RRF 而不是分数加权：
# - 向量分是余弦相似度（0~1），BM25 分是无界词频分，两个量纲没法直接相加；
#   RRF 只看"排第几"，天然免疫量纲差异，也不需要调归一化系数。
#

from __future__ import annotations

from src.core.config import get_settings

settings = get_settings()

# RRF 的平滑常数，取 60 是原论文的经验值：排名靠前的差异被放大，靠后的趋于平缓
RRF_K = 60

# 判断"是不是同一个 chunk"的指纹长度：取正文前若干字
FINGERPRINT_LEN = 80


def _fingerprint(hit: dict) -> str:
    text = hit.get("text") or hit.get("content") or ""
    return str(text)[:FINGERPRINT_LEN]


def rrf_fuse(
    ranked_lists: list[list[dict]],
    weights: list[float] | None = None,
    k: int = RRF_K,
) -> list[dict]:
    """把多路召回结果按 RRF 融合成一份有序列表。

    ranked_lists: 每一路各自的召回结果（已按该路自己的分数排好序）
    weights: 每一路的权重，默认全 1.0；想让向量占主导就给 BM25 小于 1 的权重
    返回：融合后的结果，每条带 rrf_score 与 retrievers（来源通道）
    """
    if not ranked_lists:
        return []

    if weights is None:
        weights = [1.0] * len(ranked_lists)

    agg: dict[str, dict] = {}

    for weight, hits in zip(weights, ranked_lists):
        if not hits:
            continue
        for rank, hit in enumerate(hits, start=1):
            key = _fingerprint(hit)
            if not key:
                continue

            record = agg.get(key)
            if record is None:
                record = {
                    "hit": dict(hit),
                    "rrf": 0.0,
                    "retrievers": [],
                    "best_rank": rank,
                }
                agg[key] = record

            record["rrf"] += weight / (k + rank)
            source = hit.get("retriever") or "vector"
            if source not in record["retrievers"]:
                record["retrievers"].append(source)
            record["best_rank"] = min(record["best_rank"], rank)

    fused: list[dict] = []
    for record in agg.values():
        hit = record["hit"]
        hit["rrf_score"] = round(record["rrf"], 6)
        hit["retrievers"] = record["retrievers"]
        fused.append(hit)

    fused.sort(key=lambda x: x["rrf_score"], reverse=True)
    return fused
