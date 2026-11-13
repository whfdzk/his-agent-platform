import redis.asyncio as redis
from src.core.config import get_settings

settings = get_settings()

# 生产级池参数：10k 在线下必须显式限制并具备超时/健康检查能力
_POOL_KWARGS = dict(
    max_connections=200,          # 单进程连接上限（业务+checkpointer 各 200）
    socket_timeout=5,             # 读超时（秒），防止慢命令挂起协程
    socket_connect_timeout=3,     # 建连超时（秒）
    health_check_interval=30,     # 每 30s 对空闲连接做 ping 校验
    retry_on_timeout=True,
)

# 模块级别创建连接池（应用启动时初始化一次）
redis_pool = redis.ConnectionPool(
    host=settings.REDIS_HOST,
    port=settings.REDIS_PORT,
    db=settings.REDIS_DB,
    password=settings.REDIS_PASSWORD or None,
    decode_responses=True,
    encoding="utf-8",
    **_POOL_KWARGS,
)

# 模块级别创建 Redis 客户端实例（复用连接池）; _ 开头认为是 私有变量，不暴露出去
_redis_client = redis.Redis(connection_pool=redis_pool)

# 通用的 Redis 客户端获取函数
async def get_redis_client() -> redis.Redis:
    """
    FastAPI Depends 注入用。
    直接返回模块级别的 Redis 客户端实例，不需要每次创建新实例。
    连接池会自动管理连接的获取和归还。
    """
    return _redis_client


# RedisSaver（checkpointer）专用连接池：必须 decode_responses=False（bytes 模式）
# 与业务连接池共享相同的地址配置，但保持独立的连接池，互不干扰
_checkpointer_pool = redis.ConnectionPool(
    host=settings.REDIS_HOST,
    port=settings.REDIS_PORT,
    db=settings.REDIS_DB,
    password=settings.REDIS_PASSWORD or None,
    decode_responses=False,  # RedisSaver 要求 bytes，不能 decode
    **_POOL_KWARGS,
)

# 短期记忆专用
_checkpointer_client = redis.Redis(connection_pool=_checkpointer_pool)

def get_checkpointer_redis() -> redis.Redis:
    """
    返回供 RedisSaver（LangGraph checkpointer）专用的 Redis 客户端。
    decode_responses=False，以 bytes 模式运行，与业务 Redis 客户端隔离。
    """
    return _checkpointer_client


async def close_redis_pools() -> None:
    """应用关闭时释放两个连接池，避免残留连接。"""
    try:
        await _redis_client.aclose()
    except Exception:
        pass
    try:
        await _checkpointer_client.aclose()
    except Exception:
        pass
    try:
        await redis_pool.aclose()
    except Exception:
        pass
    try:
        await _checkpointer_pool.aclose()
    except Exception:
        pass
