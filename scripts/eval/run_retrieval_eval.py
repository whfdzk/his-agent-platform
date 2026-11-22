"""检索级离线评测：算 Recall@5 / MRR，定位"分数低"到底出在哪一段。

和 scripts/eval/run_eval.py 的分工：
- run_eval.py 是端到端回归网，判定靠关键词命中，回答对不对全靠模型；
- 本脚本只测检索这一段：给定 query 和 gold 切片，看 gold 排在第几。
  这样换 embedding、调 BM25 权重、开/关 rerank 的效果能量化比较，
  不会被生成环节的措辞波动掩盖。

评测口径：
- gold 用 doc_name + chunk_index 精确到切片；
- Recall@5：gold 落在前 5 即算召回；
- MRR@5：1/rank，未召回记 0；
- 三个变体分别看"纯向量 / 向量+BM25 融合 / 再加重排"，差多少一目了然。

用法：
  python scripts/eval/run_retrieval_eval.py --variant all
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.core.config import get_settings  # noqa: E402
from src.infra.embedding import get_embeddings  # noqa: E402
from src.infra.milvus_client import get_milvus_client  # noqa: E402
from src.knowledge.bm25_store import bm25_search  # noqa: E402
from src.knowledge.reranker import rerank_docs  # noqa: E402
from src.knowledge.retrieval import rrf_fuse  # noqa: E402

COL = "knowledge_docs"
CASES_PATH = ROOT / "scripts" / "eval" / "retrieval_cases.json"
REPORT_DIR = ROOT / "doc" / "eval-reports"
OUT_FIELDS = ["doc_name", "doc_type", "page_number", "chunk_index", "text"]

settings = get_settings()


def key_of(hit: dict) -> str:
    return f"{hit.get('doc_name')}#{hit.get('chunk_index')}"


def gold_keys(case: dict) -> set[str]:
    return {f"{g['doc_name']}#{g['chunk_index']}" for g in case["gold"]}


async def vector_hits(question: str, vec: list[float], client, top_k: int, collection: str = COL) -> list[dict]:
    res = await asyncio.to_thread(
        client.search,
        collection_name=collection,
        data=[vec],
        limit=top_k,
        output_fields=OUT_FIELDS,
        search_params={"metric_type": "COSINE", "params": {"nprobe": settings.MILVUS_NPROBE}},
    )
    if not res or not res[0]:
        return []
    return [{**h["entity"], "score": float(h.get("distance", 0.0)), "retriever": "vector"} for h in res[0]]


def score_run(case: dict, hits: list[dict]) -> dict:
    golds = gold_keys(case)
    ranks = [i for i, h in enumerate(hits, start=1) if key_of(h) in golds]
    rank = min(ranks) if ranks else 0
    return {
        "id": case["id"],
        "query": case["query"],
        "gold": sorted(golds),
        "rank": rank,
        "recall5": 1 if rank and rank <= 5 else 0,
        "mrr": round(1.0 / rank, 4) if rank else 0.0,
        "top5": [key_of(h) for h in hits[:5]],
        "scores": [round(float(h.get("rerank_score") or h.get("score") or h.get("rrf_score") or 0), 4) for h in hits[:5]],
    }


async def run_variant(name: str, cases: list[dict], client, embed, collection: str = COL) -> dict:
    details = []
    t0 = time.monotonic()
    for case in cases:
        q = case["query"]
        vec = await embed.aembed_query(q)
        v = await vector_hits(q, vec, client, settings.RETRIEVAL_TOP_K, collection)
        if name == "vector":
            hits = v[:5]
        else:
            b = await bm25_search(q, client, top_k=settings.RETRIEVAL_TOP_K)
            fused = rrf_fuse([v, b], weights=[1.0, settings.BM25_WEIGHT]) if b else v
            if name == "hybrid":
                hits = fused[:5]
            else:  # hybrid_rerank
                hits = await rerank_docs(q, fused, top_k=5)
        d = score_run(case, hits)
        details.append(d)
        mark = f"rank={d['rank']}" if d["rank"] else "MISS"
        print(f"[{name:<13}] {case['id']:<4} {mark:<10} {q}")

    n = len(details)
    recall5 = sum(d["recall5"] for d in details) / n
    mrr = sum(d["mrr"] for d in details) / n
    hit1 = sum(1 for d in details if d["rank"] == 1) / n
    return {
        "variant": name,
        "n": n,
        "recall@5": round(recall5, 4),
        "mrr@5": round(mrr, 4),
        "hit@1": round(hit1, 4),
        "miss": [d["id"] for d in details if not d["rank"]],
        "seconds": round(time.monotonic() - t0, 1),
        "details": details,
    }


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--variant", default="all", choices=["vector", "hybrid", "hybrid_rerank", "all"])
    ap.add_argument("--cases", default=str(CASES_PATH))
    ap.add_argument("--tag", default="", help="报告文件名后缀，比如 before/after")
    ap.add_argument("--collection", default=COL, help="向量集合名，换 embedding 实验时指向新集合")
    args = ap.parse_args()

    data = json.loads(Path(args.cases).read_text(encoding="utf-8"))
    cases = data["cases"]
    client = get_milvus_client()
    embed = get_embeddings()

    names = ["vector", "hybrid", "hybrid_rerank"] if args.variant == "all" else [args.variant]
    results = []
    for name in names:
        print(f"\n=== {name} ===")
        results.append(await run_variant(name, cases, client, embed, args.collection))

    print("\n" + "=" * 66)
    print(f"{'变体':<16}{'Recall@5':>10}{'MRR@5':>10}{'Hit@1':>10}{'未召回':>8}")
    for r in results:
        print(f"{r['variant']:<16}{r['recall@5']:>10.3f}{r['mrr@5']:>10.3f}{r['hit@1']:>10.3f}{len(r['miss']):>8}")

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    tag = f"-{args.tag}" if args.tag else ""
    out = REPORT_DIR / f"retrieval{tag}-{stamp}.json"
    report = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "embedding_model": settings.EMBEDDING_MODEL,
        "rerank_model": settings.RERANK_MODEL,
        "top_k": settings.RETRIEVAL_TOP_K,
        "bm25_weight": settings.BM25_WEIGHT,
        "cases": str(args.cases),
        "variants": results,
    }
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告: {out}")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    asyncio.run(main())
