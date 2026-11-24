"""症状向量索引初始化脚本。

首次部署执行一次，之后 Neo4j 新增症状时可重跑——脚本幂等，会跳过已写入的症状。

用法：
    python -m src.scripts.init_symptom_index            # 增量：跳过已写入的
    python -m src.scripts.init_symptom_index --reset    # 重建：删集重来
    python -m src.scripts.init_symptom_index --dry-run  # 只取症状名看数量，不写库
"""

import argparse
import asyncio
import os
import sys

from langchain_openai import OpenAIEmbeddings

# 把项目根目录加入 Python 路径，确保能 import src 下的模块
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from neo4j import AsyncGraphDatabase
from pymilvus import (
    Collection,
    CollectionSchema,
    DataType,
    FieldSchema,
    connections,
    utility,
)

from src.core.config import get_settings

settings = get_settings()

COLLECTION_NAME = "symptom_index"
EMBEDDING_DIM = 1024  # bge-large-zh-v1.5 输出 1024 维，与 knowledge_docs 保持一致
MILVUS_ALIAS = "symptom_init"
EMBED_BATCH = 64  # 单次 embedding 请求的条数，硅基流动长文本批次别给太大


def ensure_symptom_collection(alias: str, reset: bool = False) -> Collection:
    """确保 symptom_index collection 存在；已存在直接复用，不存在则建 schema + 索引。"""
    if reset and utility.has_collection(COLLECTION_NAME, using=alias):
        utility.drop_collection(COLLECTION_NAME, using=alias)
        print(f"[INFO] --reset：已删除旧集合 '{COLLECTION_NAME}'")

    if utility.has_collection(COLLECTION_NAME, using=alias):
        print(f"[INFO] collection '{COLLECTION_NAME}' 已存在，复用（跳过已写入症状）。")
        col = Collection(COLLECTION_NAME, using=alias)
        col.load()
        return col

    fields = [
        # 用症状名作为主键，天然去重，更新时直接 upsert
        FieldSchema(name="id", dtype=DataType.VARCHAR, max_length=256, is_primary=True),
        FieldSchema(name="name", dtype=DataType.VARCHAR, max_length=256),
        FieldSchema(name="embedding", dtype=DataType.FLOAT_VECTOR, dim=EMBEDDING_DIM),
    ]
    schema = CollectionSchema(fields, description="Neo4j Symptom nodes vector index")
    col = Collection(COLLECTION_NAME, schema=schema, using=alias)
    col.create_index(
        field_name="embedding",
        index_params={
            "metric_type": "COSINE",
            "index_type": "IVF_FLAT",
            "params": {"nlist": 128},
        },
    )
    col.load()
    print(f"[INFO] collection '{COLLECTION_NAME}' 创建成功。")
    return col


async def fetch_all_symptoms(neo4j_driver) -> list[str]:
    """从 Neo4j 取出所有 Symptom 节点的名称。"""
    async with neo4j_driver.session() as session:
        result = await session.run("MATCH (s:Symptom) RETURN s.name AS name")
        records = await result.data()
    names = [r["name"] for r in records if r["name"]]
    print(f"[INFO] 从 Neo4j 获取到 {len(names)} 个症状节点。")
    return names


def get_embedding_model() -> OpenAIEmbeddings:
    """返回 Embedding 模型实例（bge-large-zh-v1.5，OpenAI 兼容接口）。

    非 OpenAI 官方模型必须关掉 tiktoken 分块，否则长文本会被截断或直接报错。
    """
    return OpenAIEmbeddings(
        model=settings.EMBEDDING_MODEL,
        api_key=settings.EMBEDDING_API_KEY,
        base_url=settings.EMBEDDING_BASE_URL,
        check_embedding_ctx_length=False,
    )


def existing_ids(col: Collection) -> set[str]:
    """读出已写入的症状 id，用于断点续跑（进度以 Milvus 自身为准，不依赖本地文件）。"""
    try:
        rows = col.query(expr='id != ""', output_fields=["id"], limit=16384)
        return {r["id"] for r in rows if r.get("id")}
    except Exception as e:  # noqa: BLE001 —— 读不到就按全量处理，最多多花 embedding
        print(f"[WARN] 读取已写入 id 失败，按全量处理：{type(e).__name__}: {e}")
        return set()


async def embed_with_retry(model, batch: list[str], retries: int = 3) -> list[list[float]]:
    """单批向量化，失败退避重试；三次都失败就抛出，由主流程中断（已完成批次已在盘上）。"""
    for attempt in range(retries):
        try:
            return await model.aembed_documents(batch)
        except Exception as e:  # noqa: BLE001
            if attempt == retries - 1:
                raise
            wait = 5 * (attempt + 1)
            print(f"[WARN] 第 {attempt + 1} 次向量化失败（{type(e).__name__}: {str(e)[:120]}），{wait}s 后重试")
            await asyncio.sleep(wait)
    raise RuntimeError("unreachable")


async def build_symptom_index(reset: bool = False, dry_run: bool = False) -> None:
    """主流程：全量构建症状向量索引，按批 upsert + flush，中断可续跑。"""
    connections.connect(alias=MILVUS_ALIAS, host=settings.MILVUS_HOST, port=settings.MILVUS_PORT)
    col = ensure_symptom_collection(MILVUS_ALIAS, reset=reset)

    neo4j_driver = AsyncGraphDatabase.driver(
        settings.NEO4J_URI,
        auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD),
    )
    symptom_names = await fetch_all_symptoms(neo4j_driver)
    await neo4j_driver.close()

    if not symptom_names:
        print("[WARN] Neo4j 中没有 Symptom 节点，请先导入医疗数据。")
        connections.disconnect(alias=MILVUS_ALIAS)
        return

    if dry_run:
        print(f"[DRY-RUN] 待向量化 {len(symptom_names)} 条，前 5 条：{symptom_names[:5]}")
        connections.disconnect(alias=MILVUS_ALIAS)
        return

    done = set() if reset else existing_ids(col)
    pending = [n for n in symptom_names if n not in done]
    print(f"[INFO] 已写入 {len(done)} 条，本次待处理 {len(pending)} 条。")
    if not pending:
        print("[INFO] 没有需要新增的症状，索引已是最新。")
        connections.disconnect(alias=MILVUS_ALIAS)
        return

    model = get_embedding_model()
    written = 0
    for i in range(0, len(pending), EMBED_BATCH):
        batch = pending[i : i + EMBED_BATCH]
        embeddings = await embed_with_retry(model, batch)
        data = [{"id": n, "name": n, "embedding": e} for n, e in zip(batch, embeddings)]
        col.upsert(data)
        col.flush()  # 每批落盘：中途断掉也不丢已完成条目
        written += len(data)
        print(f"[INFO] 进度 {written}/{len(pending)}（总计 {len(done) + written}/{len(symptom_names)}）")

    try:
        col.load()
        total = col.num_entities
    except Exception:  # noqa: BLE001
        total = len(done) + written
    print(f"[INFO] 症状索引构建完成，集合内共 {total} 条记录。")
    connections.disconnect(alias=MILVUS_ALIAS)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true", help="删除旧集合重建（默认增量续跑）")
    ap.add_argument("--dry-run", action="store_true", help="只统计症状数量，不写库")
    args = ap.parse_args()
    asyncio.run(build_symptom_index(reset=args.reset, dry_run=args.dry_run))


if __name__ == "__main__":
    main()
