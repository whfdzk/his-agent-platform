"""把魔搭中文医疗问答语料（Chinese-medical-dialogue）抽样入库到 knowledge_docs。

为什么要抽样而不是全量：
- 全量 79.9 万条，embedding 走 API 要几十万次调用，成本与时间都不允许；
- BM25 是内存索引，语料太大就得换成独立全文检索引擎，这属于下一步改造；
- 本次目的是验证"补语料能不能把库外问题变成可召回"，抽 1200 条足够看出趋势。

清洗口径：
- 答案 80~450 字，太短没信息量，太长会撑爆 chunk；
- 问句 4~40 字；
- 去掉含广告/引流字样的条目；
- 同义问句按问句前 12 字去重，避免同一知识点占满 top_k。

用法：
  python scripts/kb/ingest_ms_medical_qa.py --limit 1200
  python scripts/kb/ingest_ms_medical_qa.py --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import ijson  # noqa: E402
from loguru import logger  # noqa: E402

from src.core.config import get_settings  # noqa: E402
from src.infra.embedding import get_embeddings  # noqa: E402
from src.infra.milvus_client import get_milvus_client  # noqa: E402

DATA = ROOT / "_msdata" / "datasets" / "xiaofengalg--Chinese-medical-dialogue" / "snapshots" / "master" / "data" / "train_0001_of_0001.json"
COL = "knowledge_docs"
DOC_NAME = "中文医疗问答语料"
DOC_TYPE = "medical_qa"
CATEGORY = "全科"

AD_WORDS = ("微信", "加我", "电话咨询", "预约挂号", "在线咨询", "扫码", "私聊")
settings = get_settings()


def clean(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def pick(limit: int, stride: int) -> list[dict]:
    """流式扫描，按步长抽样，返回满足清洗口径的 QA。"""
    picked: list[dict] = []
    seen: set[str] = set()
    n = 0
    with open(DATA, "rb") as f:
        for rec in ijson.items(f, "item"):
            n += 1
            if n % stride:
                continue
            q = clean(rec.get("instruction") or "")
            detail = clean(rec.get("input") or "")
            a = clean(rec.get("output") or "")
            if not (4 <= len(q) <= 40):
                continue
            if not (80 <= len(a) <= 450):
                continue
            if any(w in a or w in q for w in AD_WORDS):
                continue
            key = q[:12]
            if key in seen:
                continue
            seen.add(key)
            text = f"问：{q}\n主诉：{detail}\n答：{a}" if detail else f"问：{q}\n答：{a}"
            picked.append({"question": q, "text": text[:600]})
            if len(picked) >= limit:
                break
    print(f"扫描 {n} 条，抽取 {len(picked)} 条")
    return picked


async def ingest(rows: list[dict], batch: int) -> int:
    client = get_milvus_client()
    embed = get_embeddings()

    # 先清掉上一轮同一来源的数据，避免重复入库
    doc_id = "msqa_chinese_medical"
    try:
        client.delete(collection_name=COL, filter=f'doc_id == "{doc_id}"')
    except Exception as e:
        logger.warning(f"清理旧数据失败（可忽略）: {e}")

    total = 0
    t0 = time.monotonic()
    for start in range(0, len(rows), batch):
        chunk = rows[start:start + batch]
        texts = [r["text"] for r in chunk]
        vecs = await embed.aembed_documents(texts)
        data = []
        for i, (t, v) in enumerate(zip(texts, vecs)):
            idx = start + i
            data.append({
                "id": f"{doc_id}_{idx:05d}",
                "doc_id": doc_id,
                "doc_name": DOC_NAME,
                "doc_type": DOC_TYPE,
                "category": CATEGORY,
                "page_number": 0,
                "chunk_index": idx,
                "text": t,
                "embedding": v,
            })
        client.insert(collection_name=COL, data=data)
        total += len(data)
        done = min(start + batch, len(rows))
        print(f"  入库 {done}/{len(rows)}  用时 {time.monotonic() - t0:.1f}s")

    client.flush(COL)
    from src.knowledge.bm25_store import invalidate
    invalidate()
    print(f"完成：{total} 个 chunk")
    return total


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=1200)
    ap.add_argument("--stride", type=int, default=40)
    ap.add_argument("--batch", type=int, default=16)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    rows = pick(args.limit, args.stride)
    if args.dry_run:
        for r in rows[:5]:
            print("-" * 50)
            print(r["text"])
        return
    await ingest(rows, args.batch)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    asyncio.run(main())
