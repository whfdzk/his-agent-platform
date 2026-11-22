"""把知识图谱里的疾病节点导出成自然语言片段，增量写入向量库。

为什么走这条路补语料：
- 图谱里已有 8801 个疾病节点、259402 条关系，属性覆盖描述/病因/预防/治疗/易感人群，
  本质是一份结构化的疾病百科，正好补向量库只有 9 篇文档的缺口；
- 数据来源可控（节点属性），不是网上爬的问诊对话，避免了"患者口述当知识"的噪声；
- 同一份数据同时服务两条通道：图谱做多跳推理，向量库做语义召回，实体名天然对齐。

片段组织原则：一段一个疾病，先概述再症状再用药，控制在 600 字内，
保证一个 chunk 就是一条完整可引用的知识。

用法：
  python scripts/kb/ingest_kg_disease_corpus.py --limit 3000
  python scripts/kb/ingest_kg_disease_corpus.py --limit 0        # 0 = 全量
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from loguru import logger  # noqa: E402
from neo4j import GraphDatabase  # noqa: E402

from src.core.config import get_settings  # noqa: E402
from src.infra.embedding import get_embeddings  # noqa: E402
from src.infra.milvus_client import get_milvus_client  # noqa: E402

COL = "knowledge_docs"
DOC_ID = "kg_disease"
DOC_TYPE = "disease_kg"
CATEGORY = "全科知识库"

settings = get_settings()

CYPHER = """
MATCH (d:Disease)
WHERE d.name IS NOT NULL AND d.description IS NOT NULL AND size(d.description) > 40
OPTIONAL MATCH (d)-[:HAS_SYMPTOM]->(s:Symptom)
OPTIONAL MATCH (d)-[:COMMON_DRUG|RECOMMEND_DRUG]->(dr:Drug)
OPTIONAL MATCH (d)-[:NEED_CHECK]->(c:Check)
OPTIONAL MATCH (d)-[:BELONGS_TO]->(dep:Department)
OPTIONAL MATCH (d)-[:ACOMPANY_WITH]->(a:Disease)
RETURN d.name AS name,
       d.description AS description, d.cause AS cause, d.prevent AS prevent,
       d.cure_way AS cure_way, d.cured_prob AS cured_prob, d.easy_get AS easy_get,
       collect(DISTINCT s.name)[0..8] AS symptoms,
       collect(DISTINCT dr.name)[0..6] AS drugs,
       collect(DISTINCT c.name)[0..5] AS checks,
       collect(DISTINCT dep.name)[0..3] AS depts,
       collect(DISTINCT a.name)[0..3] AS accompany
ORDER BY name
SKIP $skip LIMIT $limit
"""


def cut(s: str | None, n: int) -> str:
    s = (s or "").replace("\n", " ").strip()
    return s[:n]


def build_text(r: dict) -> str:
    parts = [f"疾病：{r['name']}"]
    if r.get("description"):
        parts.append(f"概述：{cut(r['description'], 200)}")
    if r.get("cause"):
        parts.append(f"病因：{cut(r['cause'], 150)}")
    if r.get("symptoms"):
        parts.append("常见症状：" + "、".join(r["symptoms"]))
    if r.get("drugs"):
        parts.append("常用药物：" + "、".join(r["drugs"]))
    if r.get("checks"):
        parts.append("相关检查：" + "、".join(r["checks"]))
    if r.get("depts"):
        parts.append("就诊科室：" + "、".join(r["depts"]))
    if r.get("easy_get"):
        parts.append(f"易感人群：{cut(r['easy_get'], 40)}")
    if r.get("cure_way"):
        parts.append(f"治疗方式：{cut(r['cure_way'], 40)}" + (f"，治愈率 {cut(r['cured_prob'], 10)}" if r.get("cured_prob") else ""))
    if r.get("prevent"):
        parts.append(f"预防：{cut(r['prevent'], 120)}")
    if r.get("accompany"):
        parts.append("可能伴随：" + "、".join(r["accompany"]))
    text = "\n".join(parts)
    return text[:600]


def fetch(limit: int) -> list[dict]:
    d = GraphDatabase.driver(settings.NEO4J_URI, auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD))
    rows = []
    with d.session() as s:
        rows = [r.data() for r in s.run(CYPHER, {"skip": 0, "limit": limit if limit > 0 else 8801})]
    d.close()
    return rows


async def ingest(rows: list[dict], batch: int) -> int:
    client = get_milvus_client()
    embed = get_embeddings()
    try:
        client.delete(collection_name=COL, filter=f'doc_id == "{DOC_ID}"')
    except Exception as e:
        logger.warning(f"清理旧数据失败（可忽略）: {e}")

    total = 0
    t0 = time.monotonic()
    for start in range(0, len(rows), batch):
        chunk = rows[start:start + batch]
        texts = [build_text(r) for r in chunk]
        try:
            vecs = await embed.aembed_documents(texts)
        except Exception as e:
            # 单批里只要有一条超长或含异常字符，整批就 400。
            # 退化成逐条调用，宁可慢一点也不让 3000 条卡在第 1 批。
            logger.warning(f"批量 embedding 失败（{type(e).__name__}），改为逐条: {str(e)[:120]}")
            vecs = [await embed.aembed_query(t[:500]) for t in texts]
        data = []
        for i, (r, t, v) in enumerate(zip(chunk, texts, vecs)):
            idx = start + i
            data.append({
                "id": f"{DOC_ID}_{idx:05d}",
                "doc_id": DOC_ID,
                "doc_name": f"疾病百科·{r['name']}"[:250],
                "doc_type": DOC_TYPE,
                "category": CATEGORY,
                "page_number": 0,
                "chunk_index": idx,
                "text": t,
                "embedding": v,
            })
        client.insert(collection_name=COL, data=data)
        total += len(data)
        print(f"  入库 {total}/{len(rows)}  用时 {time.monotonic() - t0:.0f}s", flush=True)

    client.flush(COL)
    from src.knowledge.bm25_store import invalidate
    invalidate()
    print(f"完成：{total} 个 chunk")
    return total


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=3000, help="导出条数，0 表示全量")
    ap.add_argument("--batch", type=int, default=16)
    args = ap.parse_args()

    rows = fetch(args.limit)
    print(f"取到 {len(rows)} 个疾病节点")
    if rows:
        print("样例：\n" + build_text(rows[0])[:400])
    await ingest(rows, args.batch)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    asyncio.run(main())
