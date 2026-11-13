from typing import Any, AsyncGenerator

from src.core.config import get_settings
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

settings = get_settings()

# 1. 创建引擎(连接池)
engine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.APP_DEBUG, # 调试模式下，会打印出所有的 SQL 语句
    pool_size=50, # 连接池大小（单进程；高并发下预留更多连接）
    max_overflow=30, # 超过连接池大小后，最多可以创建的连接数
    pool_timeout=30, # 连接池获取连接的超时时间，单位秒
    pool_recycle=60 * 5, # 连接池连接的最大空闲时间，单位秒
    pool_pre_ping=True, # 连接池在获取连接时，会先测试连接是否可用
    connect_args={"timeout": 15, "command_timeout": 15},  # asyncpg 建连/执行超时

)


# 2. 创建会话工厂
AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False, # 提交事务后，会话不会过期
)

# 3. 定义异步获取数据库会话连接
async def get_db() -> AsyncGenerator[AsyncSession, Any]:
    """ FastAPI 依赖注入，获取数据库会话连接 Depends(get_db)

    注意 except 必须捕 BaseException：客户端断连时框架抛的是 CancelledError
    （BaseException 子类），用 except Exception 会跳过回滚，留下悬空事务。
    流式接口（SSE）不要用这个依赖，改在短会话里取完数据即释放连接。
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session  # 返回的session别人用
            await session.commit() # 用完后自动提交事务
        except BaseException:
            try:
                await session.rollback() # 出错/被取消都回滚
            except BaseException:
                # 取消作用域里回滚本身也会再抛一次，吞掉即可（连接随会话关闭释放）
                pass
            raise