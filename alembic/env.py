from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from src.core.base_model import Base
from src.core.config import get_settings

# 这里要导入所有模型模块，确保 metadata 收集到全部表

from src.modules.medical.model import *
from src.modules.ops.model import *
from src.modules.ops.trace_model import *
from src.modules.admin.model import *
from src.modules.patient.model import *
# 漏导入会让 autogenerate 认为这些表"多余"，尝试 drop_table：
#   - chat：conversations / chat_messages（会话与消息落库）
#   - knowledge：knowledge_feedback / knowledge_notifications（反馈与知识更新通知）
from src.modules.chat.model import *
from src.knowledge.model import *
# 管理端平台域：agents / prompts / prompt_versions / system_configs / 会员三表
# 漏导入会让 autogenerate 把这些表当成"多余"而生成 drop_table。
from src.modules.platform.model import *

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 从项目配置（.env / 环境变量）读取数据库 URL，覆盖 alembic.ini 中的默认值。这样同一套迁移脚本在不同环境（dev/staging/prod）自动连不同的库。
settings = get_settings()
config.set_main_option("sqlalchemy.url", settings.DATABASE_URL)

# 所有继承 Base 的模型的表结构，就是迁移的目标状态
target_metadata = Base.metadata

# 离线模式迁移
def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True, # 参数用字面量而非绑定变量，适合生成纯 SQL 脚本
        dialect_opts={"paramstyle": "named"}, # 命名参数风格 :param
        compare_type=True, # 对比列类型变化（如 VARCHAR(50) → VARCHAR(100)）
        compare_server_default=True, # 对比服务器默认值变化（如 DEFAULT 'now' → DEFAULT 'now' ON UPDATE 'now'）
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()

# 在线模式迁移
async def run_migrations_online() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.", # 读取 ini 中 sqlalchemy. 开头的配置
        poolclass=pool.NullPool, # 不用连接池，迁移一次性执行完就关
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    import asyncio
    asyncio.run(run_migrations_online())