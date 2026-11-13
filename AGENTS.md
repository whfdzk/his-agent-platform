# 仓库指南

## 项目结构与模块组织

- `src/main.py` 是应用程序入口；`src/core/` 包含配置、共享 models、schemas、repositories、logging 和 exceptions。
- `src/agents/` 包含 supervisor agent 和 worker agents（`inquiry`、`drug`、`knowledge`、`operation` 和 `report`）。
- `src/modules/medical/` 包含医疗领域的 APIs 和 SQLAlchemy models；`src/infra/` 包含 PostgreSQL、Redis、Milvus、Neo4j 和 RustFS 的集成代码。
- `src/tools/` 只放不依赖 agent 的底层工具；调用各 worker 的工具在 `src/agents/worker_tools.py`。
- `src/middlewares/` 放 ASGI 中间件（如日志中间件）；限流等接口依赖在 `src/api/rate_limit.py`。
- `test` 包含异步 agent 测试。`scripts/` 包含数据/引导工具；`alembic/` 包含数据库 migrations。

新增领域代码请放在对应的 `src/modules/<domain>/` 包下，基础设施适配器放在 `src/infra/` 下。不要提交 `.env`、日志、缓存或生成的数据库数据。

## 构建、测试与开发命令

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python -m uvicorn src.main:app --port 8080 --reload

# 默认跑确定性单测（不连外部服务）；pytest.ini 会排除 integration 用例
pytest

# 依赖真实 LLM / 数据库的集成用例，需先确认 .env 与本地依赖就绪
pytest -m integration
```

Docker Compose 编排会启动 PostgreSQL、Redis、Milvus、Neo4j、RustFS 及配套服务：

```bash
his-agent-platform-docker compose -f doc/his-agent-platform-docker-compose.yml up -d
his-agent-platform-docker compose -f doc/his-agent-platform-docker-compose.yml down
alembic upgrade head
python scripts/init_postgres.py
```

使用 `pytest test/test_supervisor_agent.py -k memory` 运行指定测试。这些测试可能会调用已配置的 LLM 和数据库服务，请先确认 `.env` 和本地依赖是否就绪。

## 编码风格与命名规范

使用 Python 3.13 语法、四空格缩进、type annotations。`snake_case` 用于模块、函数和变量；`PascalCase` 用于类和 SQLAlchemy models；大写名称用于 settings/environment variables。注释保持简洁，仅解释不直观的医疗或基础设施相关行为。项目未配置 formatter 或 linter；格式变更请与邻近代码保持一致，避免无关的大规模重写。

## 测试指南

测试使用 `pytest` 并启用 `asyncio_mode = auto`。测试文件命名为 `test_*.py`，测试函数命名为 `test_*`。新增 agent routing、tools、APIs、migrations 和持久化行为时，请添加针对性测试。外部服务优先使用确定性的 mocks；集成测试应明确标注依赖已配置的外部服务。

## Commit 与 Pull Request 规范

近期提交历史使用简短的命令式前缀，如 `feat:`、`fix:`、`optimize:`。请遵循该模式，例如：`feat: add medication interaction tool`。Pull Request 应描述行为变更、配置或迁移步骤、测试命令及结果。若涉及 API 或面向用户的流程变更，请附请求/响应示例或截图，并明确标注 breaking changes。

## 安全与配置提示

将 `.env.example` 复制为 `.env`，凭证和 API keys 仅保留在本地。切勿提交 secrets、患者数据、生成的 service volumes 或 debug logs。在开发环境之外应用数据库 migrations 和外部服务配置前，请仔细审查。