"""覆盖缺口探针：问知识库里根本没有的题目，看 top1 分数掉不掉下来。

为什么单独做这个：13 个 chunk 的库上，Recall@5 必然是 1.0（前 5 条占全库 38%），
指标看着漂亮，但一问库外问题就露馅。真正要回答的问题是：
"库外问题，检索到的分数会不会明显低，系统有没有机会拒答。"
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.core.config import get_settings  # noqa: E402
from src.infra.embedding import get_embeddings  # noqa: E402
from src.infra.milvus_client import get_milvus_client  # noqa: E402
from src.knowledge.reranker import rerank_docs  # noqa: E402

COL = "knowledge_docs"
OUT_FIELDS = ["doc_name", "doc_type", "page_number", "chunk_index", "text"]
settings = get_settings()

# 全部是当前 9 篇文档里没有的主题
QUERIES = [
    "甲状腺功能亢进怎么治疗",
    "小孩高热惊厥怎么处理",
    "痛风急性发作吃什么药",
    "幽门螺杆菌四联疗法怎么吃",
    "糖尿病眼底病变多久筛查一次",
    "孕妇贫血吃什么补铁",
    "腰椎间盘突出保守治疗方案",
    "房颤患者抗凝药怎么选",
    "抑郁症首次发作疗程多久",
    "儿童手足口病怎么护理",
    "慢阻肺急性加重怎么用药",
    "肾结石多大需要碎石",
    "围手术期血糖控制目标",
    "哺乳期乳腺炎怎么处理",
    "过敏性紫癜需要查什么",
]


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=3)
    args = ap.parse_args()

    client = get_milvus_client()
    embed = get_embeddings()

    print(f"{'query':<26}{'vec_top1':>9}{'rr_top1':>9}  top1 文档")
    rows = []
    for q in QUERIES:
        vec = await embed.aembed_query(q)
        res = await asyncio.to_thread(
            client.search, collection_name=COL, data=[vec], limit=settings.RETRIEVAL_TOP_K,
            output_fields=OUT_FIELDS,
            search_params={"metric_type": "COSINE", "params": {"nprobe": settings.MILVUS_NPROBE}},
        )
        hits = [{**h["entity"], "score": float(h.get("distance", 0))} for h in res[0]] if res and res[0] else []
        rr = await rerank_docs(q, hits, top_k=args.top) if hits else []
        v1 = hits[0]["score"] if hits else 0.0
        r1 = float(rr[0].get("rerank_score") or 0) if rr else 0.0
        name = f"{rr[0]['doc_name']}#{rr[0]['chunk_index']}" if rr else "-"
        print(f"{q:<26}{v1:>9.3f}{r1:>9.3f}  {name}")
        rows.append({"query": q, "vec_top1": round(v1, 3), "rerank_top1": round(r1, 3), "top1": name})

    import statistics
    print("\n库外问题 top1 均值：向量 %.3f  精排 %.3f" % (
        statistics.mean(r["vec_top1"] for r in rows),
        statistics.mean(r["rerank_top1"] for r in rows),
    ))
    print("库内问题（基线集）top1 精排区间 0.541-0.846，可直接对照")

    out = ROOT / "doc" / "eval-reports" / "coverage-probe.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print("报告:", out)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    asyncio.run(main())
