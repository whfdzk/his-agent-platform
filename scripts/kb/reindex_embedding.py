"""用另一个 embedding 模型把向量库整体重建到新集合，用于 A/B 对比。

为什么需要它：换 embedding 不是改一行配置就行，向量空间整体变了，
必须重算全部 chunk 的向量；直接改 .env 会让查询向量和库里旧向量对不上，
检索结果会无预警变差。所以实验一律落在新集合里，验证有效再切正式集合。

用法：
  set EMBEDDING_MODEL=Qwen/Qwen3-Embedding-0.6B
  python scripts/kb/reindex_embedding.py --collection knowledge_docs_qwen
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
from pymilvus import DataType, MilvusClient  # noqa: E402

from src.core.config import get_settings  # noqa: E402
from src.infra.embedding import get_embeddings  # noqa: E402
from src.infra.milvus_client import get_milvus_client  # noqa: E402

SRC = "knowledge_docs"
FIELDS = ["id", "doc_id", "doc_name", "doc_type", "category", "page_number", "chunk_index", "text"]
settings = get_settings()


def create_collection(client: MilvusClient, name: str, dim: int) -> None:
    if client.has_collection(name):
        client.drop_collection(name)
    schema = MilvusClient.create_schema(auto_id=False)
    schema.add_field("id", DataType.VARCHAR, max_length=256, is_primary=True)
    schema.add_field("doc_id", DataType.VARCHAR, max_length=128)
    schema.add_field("doc_name", DataType.VARCHAR, max_length=256)
    schema.add_field("doc_type", DataType.VARCHAR, max_length=50)
    schema.add_field("category", DataType.VARCHAR, max_length=100)
    schema.add_field("page_number", DataType.INT64)
    schema.add_field("chunk_index", DataType.INT64)
    schema.add_field("text", DataType.VARCHAR, max_length=65535)
    schema.add_field("embedding", DataType.FLOAT_VECTOR, dim=dim)
    idx = MilvusClient.prepare_index_params()
    idx.add_index(field_name="embedding", metric_type="COSINE", index_type="IVF_FLAT", params={"nlist": 128})
    client.create_collection(collection_name=name, schema=schema, index_params=idx)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--collection", default="knowledge_docs_alt")
    ap.add_argument("--batch", type=int, default=8)
    args = ap.parse_args()

    client = get_milvus_client()
    embed = get_embeddings()
    print("embedding model:", settings.EMBEDDING_MODEL)

    # Milvus 单次查询窗口上限 16384，必须分页取，否则大库直接报 invalid max query result window
    rows: list[dict] = []
    page = 4000
    offset = 0
    while True:
        batch = client.query(
            collection_name=SRC, filter="chunk_index >= 0", output_fields=FIELDS,
            limit=page, offset=offset,
        )
        if not batch:
            break
        rows.extend(batch)
        offset += page
        if len(batch) < page:
            break
    print("待重建 chunk 数:", len(rows))

    dim = len(await embed.aembed_query("测试"))
    create_collection(client, args.collection, dim)

    t0 = time.monotonic()
    for start in range(0, len(rows), args.batch):
        batch = rows[start:start + args.batch]
        texts = [r["text"] for r in batch]
        try:
            vecs = await embed.aembed_documents(texts)
        except Exception as e:
            logger.warning(f"批量失败，逐条重试: {str(e)[:100]}")
            vecs = [await embed.aembed_query(t[:500]) for t in texts]
        data = [{**{k: r[k] for k in FIELDS}, "embedding": v} for r, v in zip(batch, vecs)]
        client.insert(collection_name=args.collection, data=data)
        print(f"  {min(start + args.batch, len(rows))}/{len(rows)}  {time.monotonic() - t0:.0f}s", flush=True)

    client.flush(args.collection)
    print("完成 ->", args.collection)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    asyncio.run(main())
