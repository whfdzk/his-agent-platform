from loguru import logger
from pymilvus import MilvusClient, connections, utility

from src.core.concurrency import to_thread
from src.core.config import get_settings

settings = get_settings()
MILVUS_ALIAS = "his_milvus"

# 进程级共享的 MilvusClient（MilvusClient 内部持有 gRPC 连接，必须复用而非每请求新建）
_shared_client: MilvusClient | None = None


def get_milvus_client() -> MilvusClient:
    """返回进程级共享的 MilvusClient。

    与 get_milvus_client_alias() 的区别：
    - alias 走 pymilvus 的 ORM Collection API（MilvusStore 长期记忆用）
    - 这里走 MilvusClient 高层 API（知识库检索/导入用）
    两者各自持有连接，不可混用。
    """
    global _shared_client
    if _shared_client is None:
        _shared_client = MilvusClient(
            uri=f"http://{settings.MILVUS_HOST}:{settings.MILVUS_PORT}"
        )
    return _shared_client


def get_milvus_client_alias() -> str:
    """
    返回 Milvus 连接别名。
    若连接不存在会自动创建，供业务层复用。
    """
    connections.connect(
        alias=MILVUS_ALIAS,
        host=settings.MILVUS_HOST,
        port=settings.MILVUS_PORT,
    )
    return MILVUS_ALIAS


async def aget_milvus_client_alias() -> str:
    """异步版：connections.connect 是同步 IO，必须挪出 event loop。"""
    return await to_thread(get_milvus_client_alias)


def get_milvus_dependency() -> str:
    """
    FastAPI Depends 注入用。
    返回可复用的 Milvus 连接别名。
    """
    return get_milvus_client_alias()


def check_milvus_health() -> bool:
    """
    检查 Milvus 连通性。
    通过拉取集合列表验证连接是否可用。
    """
    alias = get_milvus_client_alias()
    collections = utility.list_collections(using=alias)
    logger.info(f"Milvus 连接成功，当前集合数量：{len(collections)}")
    return True


def close_milvus_client() -> None:
    """关闭 Milvus 连接（lifespan 关闭时调用）。"""
    global _shared_client
    connections.disconnect(alias=MILVUS_ALIAS)
    if _shared_client is not None:
        try:
            _shared_client.close()
        except Exception as e:
            logger.warning(f"MilvusClient 关闭失败: {e}")
        _shared_client = None
