"""比较两个精排模型在同一批候选上的排序效果。

为什么单独做这一步：精排只在候选池上重排，不改变召回，因此可以在同一份向量结果上
直接对比 qwen3-rerank（DashScope）与 bge-reranker-v2-m3（硅基流动），
不需要为每个精排模型重建集合，便宜的实验就先做完再决定要不要改代码。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from src.core.config import get_settings  # noqa: E402
from src.infra.embedding import get_embeddings  # noqa: E402
from src.infra.milvus_client import get_milvus_client  # noqa: E402

settings = get_settings()


async def dashscope_rerank(query: str, docs: list[dict], top_k: int) -> list[dict]:
    import dashscope
    from dashscope import TextReRank

    dashscope.api_key = settings.DASHSCOPE_API_KEY
    resp = TextReRank.call(
        model="qwen3-rerank", query=query, documents=[d["text"] for d in docs],
        top_n=top_k, return_documents=False,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"dashscope rerank: {resp.message}")
    out = []
    for item in resp.output.results:
        d = docs[item.index].copy()
        d["rr"] = item.relevance_score
        out.append(d)
    return out


async def siliconflow_rerank(query: str, docs: list[dict], top_k: int, model: str) -> list[dict]:
    headers = {"Authorization": f"Bearer {settings.EMBEDDING_API_KEY}"}
    payload = {
        "model": model,
        "query": query,
        "documents": [d["text"] for d in docs],
        "top_n": top_k,
        "return_documents": False,
    }
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(f"{settings.EMBEDDING_BASE_URL}/rerank", json=payload, headers=headers)
        r.raise_for_status()
        body = r.json()
    out = []
    for item in body["results"]:
        d = docs[item["index"]].copy()
        d["rr"] = item["relevance_score"]
        out.append(d)
    return out


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--collection", default="knowledge_docs")
    ap.add_argument("--pool", type=int, default=30)
    ap.add_argument("--top-k", type=int, default=5)
    args = ap.parse_args()

    cases = json.load(open(ROOT / "scripts/eval/retrieval_cases.json", encoding="utf-8"))["cases"]
    client = get_milvus_client()
    embed = get_embeddings()

    # 先把所有候选取出来缓存住，两个精排模型吃同一批候选，比较才成立
    pool = []
    for case in cases:
        vec = await embed.aembed_query(case["query"])
        res = await asyncio.to_thread(
            client.search, collection_name=args.collection, data=[vec], limit=args.pool,
            output_fields=["doc_name", "doc_type", "page_number", "chunk_index", "text"],
            search_params={"metric_type": "COSINE", "params": {"nprobe": settings.MILVUS_NPROBE}},
        )
        pool.append([dict(h["entity"]) for h in (res[0] if res else [])])

    def hit_rank(golds: list[dict], ranked: list[dict]) -> int:
        for i, d in enumerate(ranked, 1):
            for g in golds:
                if d["doc_name"] == g["doc_name"] and int(d["chunk_index"]) == int(g["chunk_index"]):
                    return i
        return 0

    models = [
        ("qwen3-rerank", lambda q, d: dashscope_rerank(q, d, args.top_k)),
        ("bge-reranker-v2-m3", lambda q, d: siliconflow_rerank(q, d, args.top_k, "BAAI/bge-reranker-v2-m3")),
    ]

    summary = []
    for name, fn in models:
        recalls, mrrs = [], []
        t0 = time.monotonic()
        for case, docs in zip(cases, pool):
            try:
                ranked = await fn(case["query"], docs)
            except Exception as e:
                print(f"[{name}] {case['query'][:12]} 失败: {str(e)[:100]}")
                ranked = docs[: args.top_k]
            r = hit_rank(case["gold"], ranked)
            recalls.append(1 if 0 < r <= args.top_k else 0)
            mrrs.append(1.0 / r if r else 0.0)
        summary.append({
            "model": name,
            "recall@5": sum(recalls) / len(cases),
            "mrr@5": sum(mrrs) / len(cases),
            "seconds": round(time.monotonic() - t0, 1),
        })

    print(f"\n{'精排模型':<22}{'Recall@5':>9}{'MRR@5':>8}{'耗时':>8}")
    for s in summary:
        print(f"{s['model']:<22}{s['recall@5']:>9.3f}{s['mrr@5']:>8.3f}{s['seconds']:>7.0f}s")

    out = ROOT / "doc/eval-reports" / f"rerank-compare-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps({"collection": args.collection, "summary": summary}, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print("报告:", out)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    asyncio.run(main())
