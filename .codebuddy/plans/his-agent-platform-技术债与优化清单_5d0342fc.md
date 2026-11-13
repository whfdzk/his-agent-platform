---
name: his-agent-platform-技术债与优化清单
overview: 对 his-agent-platform 从系统架构/耦合、性能并发、封装冗余三个维度做全面体检，输出带文件位置与优先级（P0/P1/P2）的整改清单与分阶段实施路线。
todos:
  - id: fix-p0-safety-net
    content: 修正 pytest.ini 目录名与矛盾断言，为 chat/inquiry/nl2sql 补确定性单测安全网
    status: completed
  - id: fix-p0-data-and-sse
    content: 修复 persist_queue 丢数据，SSE 路径接入 guarded 并去掉每 token 的 Redis incr
    status: completed
    dependencies:
      - fix-p0-safety-net
  - id: unify-clients-and-config
    content: 单例化 LLM/Embedding/Milvus 客户端，删除 api.py 的 _build_client，配置收拢进 Settings
    status: completed
    dependencies:
      - fix-p0-safety-net
  - id: unblock-event-loop
    content: 把 milvus/rustfs/文档解析的同步 IO 包成 to_thread，stats 聚合下推 SQL 并补索引
    status: completed
    dependencies:
      - unify-clients-and-config
  - id: break-coupling
    content: 建 agents/registry 打破 tools 与 agents 循环依赖，消除 core 倒挂与关键延迟 import
    status: completed
    dependencies:
      - unify-clients-and-config
  - id: split-god-files
    content: 拆分 chat.py 的 _event_generator 与 inquiry/graph.py，抽出 SseWriter、TurnOrchestrator 与节点模块
    status: completed
    dependencies:
      - break-coupling
  - id: api-contract-and-errors
    content: 为全部路由补 response_model 与统一错误码，改抛 BizException 并去除裸 HTTPException
    status: completed
    dependencies:
      - split-god-files
  - id: cleanup-and-docs
    content: 清理死代码、worker 工厂化、移除明文密码与失效注释、将 rate_limit 移到 api/deps
    status: completed
    dependencies:
      - api-contract-and-errors
---

## 需求概述
对 `his-agent-platform` 做一次全身体检，从**系统架构、代码耦合冗余、性能优化、代码封装**四个维度排查，输出一份带文件定位、影响说明与优先级的整改清单，并按阶段给出可执行的实施路线。

## 核心内容
- 架构层：识别分层倒挂、循环依赖、上帝文件、抽象缺失
- 性能层：识别事件循环阻塞、连接/客户端泄漏、N+1、缓存缺失、并发保护缺口
- 封装层：识别配置散落、异常体系空转、API 契约缺失、重复代码与死代码、测试基建失效
- 输出形态：每条问题含「文件:行号 + 影响 + 建议做法」，标注 P0（线上事故/数据丢失）/ P1（明显收益）/ P2（长期可维护性），并给出分阶段实施顺序


## 体检结论总览
已逐条核实证据（含修正一处误判：`get_llm` 底层 httpx 客户端已被 `@lru_cache` 复用，真正的开销是 `ChatOpenAI`/`OpenAIEmbeddings` 对象每轮重建，而非连接池重建）。共 26 项问题：**P0 六项、P1 十一项、P2 九项**。

---

## 一、系统架构与代码耦合

| # | 优先级 | 问题定位 | 影响 | 建议做法 |
|---|---|---|---|---|
| A1 | P0 | **真循环依赖**：`src/tools/worker_tools.py:10-18` import `agents.inquiry.graph` + 5 个 worker；反向 `src/agents/supervisor_agent.py:8-13` import `src.tools.*` | 最底层的 tools 反向依赖 agents，模块无法单独导入与测试，改动 agent 会波及工具层 | 抽 `src/agents/registry.py` 作为 worker 注册表；tools 只依赖注册表接口，用依赖注入替代静态 import |
| A2 | P1 | **core 倒挂依赖上层**：`src/core/persist_queue.py:57` import infra.database、`:67,87` import modules.* | 最底层依赖上层，分层失效，core 无法复用 | persist_queue 改为只接收「已构造好的持久化回调」，由 `main.py` 在启动时注入具体实现 |
| A3 | P1 | **上帝文件**：`src/agents/inquiry/graph.py`（约 780 行，9 node + 7 route 编排 / LLM / Neo4j / Milvus / DB 持久化 / 依赖工厂混杂，最长函数 `run_inquiry` 550-625） | 任何改动都触碰核心文件，无法单测、无法并行开发 | 拆为 `graph.py`（纯编排）+ `nodes/*.py`（各节点）+ `deps.py`（依赖工厂）+ 复用现有 `db_queries.py`/`neo4j_queries.py` |
| A4 | P1 | **上帝函数**：`src/api/routers/chat.py:391-559` `_event_generator`（169 行，内含嵌套闭包 `run_turn` 430-474），单函数混合 SSE 传输/心跳/超时/幂等重放/Redis 锁/seq 分配/落库/双分支 agent 编排/降级/断线续传 | 无法测试、无法复用，任意一环出错都会连带影响流式主链路（当前最热路径） | 抽出 `SseWriter`（封装心跳/seq/事件序列化）、`TurnOrchestrator`（封装锁+幂等+落库+编排），router 只负责组装 |
| A5 | P2 | **agent 直插 ORM**：`src/agents/inquiry/db_queries.py:7` import `src.modules.medical.model` | agent 层直接持有表模型，modules 无法独立演进 | 在 `modules/` 暴露查询函数，agent 只调用领域接口 |
| A6 | P2 | **延迟 import 掩盖问题**：35 处函数内 import（`knowledge/tools.py:37,51,70,90,105,128`、`core/persist_queue.py:57,67,87`、`core/llm_callbacks.py:138`、`api/routers/chat.py:169,207,571`、`agents/inquiry/graph.py:63,405,646`） | 把导入期错误推迟到运行期，线上才炸 | A1/A2 解环后，逐步把可上提的改为顶层 import |
| A7 | P1 | **抽象缺失**：`src/core/base_repository.py:9` `BaseRepository`、`src/core/base_schema.py:8` `ResponseSchema` 全仓零引用；10+ 处裸 `AsyncSessionLocal()` 绕过 repository（`chat.py:213,261,575`、`session.py:85,138`、`tools/worker_tools.py:67`、`tools/patient_tools.py:36`、`agents/inquiry/db_queries.py:27,103`） | 基类是空壳，数据访问无统一入口，事务边界不清 | 二选一：要么让 `modules/` 下 repository 真正继承并迁移裸 Session 调用，要么直接删除空壳避免误导 |

---

## 二、性能优化

| # | 优先级 | 问题定位 | 影响 | 建议做法 |
|---|---|---|---|---|
| P1 | P0 | **SSE 路径绕过并发保护**：`guarded` 只用在 `chat.py:317,324`（非流式）与 `tools/worker_tools.py:70,112,130,148,166`；流式 `chat.py:450 agent.astream(...)` 与 `:434 _run_inquiry_turn` **完全没有 guarded** | 流式是主链路，却绕过 `LLM_MAX_CONCURRENCY` 与 150s 总超时保护，流量高峰会打爆 LLM 网关 | 在 `run_turn()` 闭包外层包 `guarded(...)`，保留流式语义的同时纳入信号量与超时 |
| P2 | P0 | **SSE 每 token 一次 Redis 往返**：`chat.py:510` `seq = await redis.incr(...)` 在 token 循环内 | 200 token 回复 = 200 次串行 Redis RTT，直接拖死流式吞吐 | seq 改为本地计数器自增（`turn_id` 内序号），或批量 incr（如 `incrby`）后本地切分 |
| P3 | P0 | **persist_queue 丢数据**：`src/core/persist_queue.py:50` 队列满即丢弃；`:129` `finally: batch = []` 在异常路径清空批次 | 聊天消息与 token 用量会静默丢失，计费/审计数据不准 | 队列满改为阻塞背压（`put` 等待）或落盘兜底；异常路径把 batch 重新入队而非清空 |
| P4 | P1 | **async 内同步阻塞**（全仓 0 处 `to_thread`/`run_in_executor`，已 grep 验证）：`src/infra/milvus_store.py:151,159,162,170,179,205,214`（同步 pymilvus gRPC）；`src/knowledge/doc_ingestion.py:124-127,137,176,212,240`（含 `SimpleDirectoryReader.load_data()`）；`src/api/routers/knowledge.py:55,60`；`src/infra/rustfs_client.py:83,100,120`；`src/knowledge/doc_rag.py:41,125,128` | 阻塞期间整个 worker 进程所有请求停摆，是该架构下最典型的并发杀手 | 统一用 `anyio.to_thread.run_sync(...)` 包裹；或在 `infra/` 层提供 async 适配（Milvus 用 `AsyncMilvusClient` 或线程池） |
| P5 | P1 | **Milvus 客户端每请求新建且不关闭**：`src/api/routers/knowledge.py:22-34` | gRPC 连接泄漏，长跑后句柄耗尽 | 改为模块级单例 + lifespan 关闭，复用现有 `src/infra/milvus_client.py` |
| P6 | P1 | **Embedding/LLM 客户端重复构造 3 份**：`agents/supervisor_agent.py:29-34`、`agents/inquiry/graph.py:648-654`、`api/routers/knowledge.py:23-28`（参数完全一致，`base_url` 硬编码 2 处） | 对象每轮重建，且换供应商要改 3 个文件 | 统一到 `src/infra/embedding.py` 单例工厂，base_url 走 Settings 新增的 `EMBEDDING_BASE_URL` |
| P7 | P1 | **LLM 客户端优先级冲突**：`core/config.py:144` 是 `DEEPSEEK>OPENAI>DASHSCOPE`，而 `api/routers/api.py:11-22` `_build_client()` 是 `OPENAI>DEEPSEEK>DASHSCOPE` | 同一份 `.env` 在不同代码路径可能选出不同厂商，且 api.py 路径绕过 `llm_callbacks`，token 用量不入库 | 删除 `_build_client()`，改为调用 `get_llm()`/统一工厂，确保计量回调贯穿 |
| P8 | P1 | **stats 大结果集拉到 Python 聚合**：`src/api/routers/stats.py:59` `.limit(20000)`、`:99` `.limit(2000)`；`:53` 用字符串拼 `f"{end} 23:59:59"` 无法命中索引；`:143-170` 三次 GROUP BY 串行 await；`TokenUsage.created_at` 无索引 | 全表扫描 + 内存聚合 + 3 倍 RTT，随数据量增长线性劣化 | 改为 SQL `GROUP BY` 下推、时间条件用 `datetime` 比较、`asyncio.gather` 并发三条聚合、alembic 补 `created_at` 与 `session_id` 复合索引 |
| P9 | P2 | **缓存能力缺失**：`src/infra/redis_cache.py` 只有裸客户端；LLM 结果、embedding、知识库检索、nl2sql 结果均未缓存；`src/knowledge/nl2sql.py:57` 重试循环内最多 3 次重新调 LLM | 同一问题重复付费调 LLM，成本与延迟双高 | 基于现有 redis 客户端封装 `@cached(ttl)` 装饰器，优先对 embedding 与 nl2sql（按问题 hash）落地 |
| P10 | P2 | **Redis 键空间风险**：`api/routers/session.py:68` user_id 为空时 `scan_iter` 扫描全库 keyspace；`session.py:69-81` `scan_iter` + 每 key 一次 `get`（N+1） | keyspace 变大后单次请求阻塞 Redis | 用集合结构（如 `user:{id}:sessions`）替代 keyspace 扫描；批量 `mget` 替代循环 `get` |
| P11 | P2 | **串行 await 可并行**：`agents/inquiry/neo4j_queries.py:100-104` 4 条 Cypher；`knowledge/graph_rag.py:62→66`、`fusion.py:91,97` 链式 LLM | 延迟叠加 | 无依赖的调用改 `asyncio.gather`；NL2SQL 与文档检索的融合（fusion.py）已是现成的并发点，可优先改 |
| P12 | P2 | **Neo4j 连接池未配上限**：`src/infra/neo4j_client.py:16` 未设 `max_connection_pool_size` / `connection_acquisition_timeout` | 默认池 100 且获取连接无限等待，DB 抖动时会雪崩 | 显式配置池大小与获取超时 |

---

## 三、代码封装与冗余

| # | 优先级 | 问题定位 | 影响 | 建议做法 |
|---|---|---|---|---|
| E1 | P0 | **测试基建失效**：`pytest.ini:9` 写 `testpaths = tests`，实际目录是 `test` → 裸跑 `pytest` 收集 0 用例（AGENTS.md:20 用 `pytest test` 掩盖了该 bug）；`test/` 仅 2 文件约 94 行；inquiry graph、chat router、nl2sql、prescription_review 全部零覆盖；`test/test_supervisor_agent.py:53` 断言 `"糖尿病" in resp2` 但自身 fixture 种的是「新冠病史/樱桃过敏」，断言与 fixture 矛盾 | 重构没有安全网，后续所有改动都无法验证 | 先修 `pytest.ini`，再补确定性单测（外部服务用 mock），并修正自相矛盾的断言 |
| E2 | P1 | **API 无契约**：全 src 仅 1 处 `response_model=`（`chat.py:287`），其余全部返回裸 dict（`session.py:110/186`、`main.py:231`、`api.py:60`、stats/appointment/auth/model_config/knowledge） | 无输出校验、OpenAPI 只有 200 空壳、前端无类型契约 | 用 `core/base_schema.py` 的 `ResponseSchema` 统一信封，逐个路由补 `response_model` |
| E3 | P1 | **异常体系空转**：`core/exceptions.py:8-18` 的 `ERROR_CODE_400/404/500`、`ERR_PERM_NOT_FOUND` 零引用；统一信封仅被 `config.py:146` 用到 1 次；API 层 17 处裸抛 `HTTPException`；30 处 `except Exception as e:`（main.py 独占 9 处），其中 `main.py:61-62/70-71/78-79` 为 `except Exception: pass` | 前端只能按 HTTP status 分支；内部异常细节外泄（`api.py:73`、`knowledge.py:116,143`）；静默吞错导致故障不可见 | 建立错误码枚举 + 全局 handler 统一信封；API 层改抛 `BizException`；吞异常处至少改为 `logger.warning` 并记录关键上下文 |
| E4 | P1 | **配置散落**：硅基流动 URL 硬编码 6 处（`api/routers/knowledge.py:26`、`agents/supervisor_agent.py:32`、`agents/inquiry/graph.py:651` + 3 个脚本）；Settings 无 `EMBEDDING_BASE_URL`、无 `RERANK_MODEL`（`knowledge/reranker.py:38` 硬编码 `qwen3-rerank`）；检索阈值四散（`doc_rag.py:19-20,37`、`graph_rag.py:71`、`fusion.py:70/76/81`、`confidence.py:85,91`）；分页魔法数散落（`session.py:86,110,154`、`stats.py:59,99,190`、`appointment.py:103`、`chat.py:588`） | 调参无统一入口，换供应商要改 6 个文件 | 全部收拢进 `core/config.py` 的 `Settings`，按域分组（检索域/分页域/超时域） |
| E5 | P2 | **敏感默认值与文档泄漏**：`config.py:24` DB_PASSWORD="postgres"、`:49` NEO4J_PASSWORD="medical123"、`:79` JWT_SECRET_KEY="" → `utils/jwt_utils.py:20` 回退硬编码 `"dev-only-insecure-secret"`；README.md:41-47 明文写出三套密码 | 生产环境误用默认值即为安全事件 | 敏感项改为必填（缺失即启动失败），至少在 `APP_ENV != dev` 时强制校验；README 去除明文密码 |
| E6 | P2 | **重复实现**：PHI 脱敏 2 套（`utils/masking.py:8-28` vs `main.py:41-42` 自造正则）；conversation 解析 2 份（`chat.py:165-192` vs `session.py:139-144`）；Milvus 客户端 3 套 | 同一逻辑两处维护，容易改漏 | 统一到 `utils/masking.py` 与 `infra/milvus_client.py` |
| E7 | P2 | **死代码**：`src/knowledge/tools.py`（148 行，`build_knowledge_tools`/`KnowledgeDeps` 全仓零引用）、`src/knowledge/conversation.py`、`src/core/base_repository.py`、`src/core/base_schema.py`（后两个视 A7 决策）；`src/agents/workers/knowledge_agent.py:26` 是 `tools = []`；死配置 `config.py:66 VL_MODEL` | 误导维护者，其中 RAG 工具链是「实现了但没接线」的半成品 | 确认是「待接线」还是「废弃」：接线则补上 `tools` 引用，废弃则删除 |
| E8 | P2 | **4 个 worker 是同一模板复制**：drug/knowledge/operation/report（各约 1.1KB，仅常量 PROMPT 与名字不同） | 典型复制粘贴债务 | 抽 `create_worker(name, prompt, tools)` 工厂 + `global` 注册表，4 个文件各缩到 10 行 |
| E9 | P2 | **文档与代码不符**：AGENTS.md:9 的 `scripts/medical.json`、:26 与 README 的 docker-compose yml 在仓库中不存在；`chat.py:7` 注释「单轮 90s」vs `config.py:72` TURN_TIMEOUT=150；`supervisor_agent.py:151,154` 注释「4 条」实为 6；`src/middlewares/rate_limit.py` 并非中间件（实为 `chat.py:35` 导入的依赖函数） | 新人按文档操作会直接踩坑；文件位置误导 | 修正文档路径与失效注释，把 `rate_limit.py` 移到 `api/deps/` 或 `core/` |
| E10 | P2 | **提示词仍是 .py 常量**：优点是常量 + `.format()`（无超长 f-string 拼装，这点做得对）；但品牌名 4 种并存（"天宫医疗"/"平台医疗"/"医疗平台"），4 个 QA 提示词尾部重复「直接输出回答内容」 | 改提示词需发版，品牌文案不一致 | 抽取统一的 `BRAND_NAME` 常量与公共尾部片段；按 `prompts.py:243-244` 已有规划，中期可考虑 Redis/DB 加载 |

---

## 四、实施路线（按风险收益排序）

**第一阶段：止血（P0）** — 修 `pytest.ini` 与测试断言 → 补确定性单测安全网 → persist_queue 数据可靠性 → SSE 并发保护与每 token Redis incr。此阶段必须完成，否则后续重构无法验证。

**第二阶段：统一基础设施（P1）** — 单例化 LLM/Embedding/Milvus 客户端并收拢配置 → 同步阻塞改 `to_thread` → stats 聚合下推 + 补索引。此阶段收益最直接、风险可控。

**第三阶段：解耦（P1/P2）** — 打破 tools↔agents 环与 core 倒挂 → 拆分两个上帝文件 → 补 API 契约与错误码。

**第四阶段：清理（P2）** — 死代码处置、worker 工厂化、文档与注释对齐、缓存层落地。

> 每阶段独立可交付、独立回滚，不做跨阶段的大规模重写。

## 五、涉及文件清单

```
src/
├── core/
│   ├── config.py              # [MODIFY] 新增 EMBEDDING_BASE_URL / RERANK_MODEL / 检索与分页阈值分组；敏感项改为生产环境必填校验
│   ├── concurrency.py         # [MODIFY] 复用现有 guarded；不改接口，仅由 chat.py 在流式路径调用
│   ├── persist_queue.py       # [MODIFY] 队列满改背压、异常路径重新入队；移除 :57/:67/:87 对 infra/modules 的倒挂 import，改由 main.py 注入回调
│   ├── exceptions.py          # [MODIFY] 启用错误码枚举，移除零引用常量，统一 {"code","message","data"} 信封
│   ├── base_schema.py         # [MODIFY or DELETE] 视 A7 决策：供 E2 使用则保留并继承，否则删除
│   ├── base_repository.py     # [MODIFY or DELETE] 视 A7 决策
│   └── llm_callbacks.py       # [MODIFY] 消除 :138 延迟 import
├── infra/
│   ├── milvus_store.py        # [MODIFY] :151/159/162/170/179/205/214 同步 pymilvus 调用包 to_thread
│   ├── milvus_client.py       # [MODIFY] 暴露模块级单例供 knowledge router 复用
│   ├── rustfs_client.py       # [MODIFY] :83/:100/:120 同步 boto3 调用包 to_thread
│   ├── neo4j_client.py        # [MODIFY] 补 max_connection_pool_size / connection_acquisition_timeout
│   └── redis_cache.py         # [MODIFY] 新增 @cached(ttl) 装饰器封装（E9 缓存落地）
├── api/
│   ├── routers/
│   │   ├── chat.py            # [MODIFY][重点] 拆出 SseWriter 与 TurnOrchestrator；流式路径接入 guarded；seq 改本地计数；补 response_model
│   │   ├── api.py             # [MODIFY] 删除 :11-22 _build_client，改调 get_llm 使 token 计量入库
│   │   ├── stats.py           # [MODIFY] 聚合下推 SQL、时间条件改 datetime、三次 gather、补索引
│   │   ├── session.py         # [MODIFY] keyspace 扫描改集合结构、循环 get 改 mget、补 response_model
│   │   ├── knowledge.py       # [MODIFY] 单例化 Milvus/Embedding、:55/:60 文件 IO 包 to_thread、补 response_model
│   │   ├── appointment.py     # [MODIFY] 改抛 BizException、补 response_model
│   │   ├── auth.py            # [MODIFY] 改抛 BizException、补 response_model
│   │   └── model_config.py    # [MODIFY] 改抛 BizException、补 response_model
│   └── deps.py                # [MODIFY] 承接从 middlewares/ 迁移来的限流依赖（E9）
├── agents/
│   ├── registry.py            # [NEW] worker 注册表，供 A1 打破 tools↔agents 环
│   ├── supervisor_agent.py    # [MODIFY] 改依赖 registry；复用统一 embedding 单例；修正失效注释
│   ├── inquiry/
│   │   ├── graph.py           # [MODIFY][重点] 拆为纯编排，节点外移，依赖工厂外移
│   │   ├── nodes/             # [NEW] 由 graph.py 拆出的各节点实现
│   │   ├── deps.py            # [NEW] 依赖工厂 + Milvus 单例（承接 :629-665）
│   │   ├── neo4j_queries.py   # [MODIFY] :100-104 四条 Cypher 改 gather
│   │   └── db_queries.py      # [MODIFY] 改为调用 modules 领域接口，不再直插 ORM
│   └── workers/
│       ├── factory.py         # [NEW] create_worker 工厂（E8）
│       └── drug/knowledge/operation/report_agent.py  # [MODIFY] 各缩至约 10 行；knowledge_agent 补 tools 引用
├── tools/
│   └── worker_tools.py        # [MODIFY] 改为依赖 registry，:10-18 顶层 import 移除
├── knowledge/
│   ├── tools.py               # [DELETE or MODIFY] 接线则被 knowledge_agent 引用，否则删除
│   ├── conversation.py        # [DELETE]
│   ├── doc_ingestion.py       # [MODIFY] :124-127/137/176/212/240 同步解析与 IO 包 to_thread
│   ├── doc_rag.py             # [MODIFY] :41/125/128 同步 search 包 to_thread；阈值入 Settings
│   ├── nl2sql.py              # [MODIFY] 重试循环加缓存，避免重复付费
│   ├── reranker.py            # [MODIFY] :38 硬编码模型名改读 Settings.RERANK_MODEL
│   ├── fusion.py              # [MODIFY] 检索与融合改并发
│   └── prompts.py             # [MODIFY] 统一 BRAND_NAME 与公共提示词片段
├── middlewares/
│   └── rate_limit.py          # [MOVE] 不是中间件，移至 api/deps/
├── utils/masking.py           # [MODIFY] 供 main.py 复用，消除第二套脱敏
└── main.py                    # [MODIFY] lifespan 初始化抽为统一 bootstrap；脱敏复用 utils；异常不再 pass；health_deps 同步 IO 包 to_thread

# 仓库根
├── pytest.ini                 # [MODIFY][P0] testpaths: tests → test
├── test/                      # [MODIFY] 修正 test_supervisor_agent.py:53 矛盾断言；补 chat/inquiry/nl2sql 确定性单测
├── alembic/versions/          # [NEW] 补 token_usage(created_at, session_id) 等索引迁移
├── AGENTS.md / README.md      # [MODIFY] 修正失效路径、去除明文密码、统一测试命令
└── .env.example               # [MODIFY] 补齐缺失配置项（JWT_SECRET_KEY / EMBEDDING_* / MINERU_* / LLM_* / CORS_ORIGINS 等）
```

## 六、关键接口约定

为保证第三阶段解耦不破坏现有调用方，先固定两个契约：

```python
# src/agents/registry.py —— 打破 tools ↔ agents 环的关键抽象
# tools 层只依赖此协议，不再静态 import 具体 agent 模块
class AgentProvider(Protocol):
    def get(self, name: str) -> Any: ...          # 返回已注册的 agent 实例
    def register(self, name: str, factory: Callable[[], Any]) -> None: ...

# src/core/concurrency.py —— 复用现有 guarded 签名，不改接口
async def guarded(coro: Awaitable[T], timeout: float | None = None) -> T: ...

# src/infra/redis_cache.py —— 新增缓存装饰器（E9/P9 落地用）
def cached(key_prefix: str, ttl: int = 300): ...  # 仅缓存可序列化结果，禁止缓存含 PHI 的内容
```

**性能与安全约束**：
- 缓存 key 必须按租户/用户隔离，且**禁止缓存含 PHI 字段**的内容（与 `main.py` 现有 Sentry 脱敏策略一致）
- `to_thread` 改造需控制线程池并发上限，避免 IO 密集型任务把默认线程池打满
- 所有对外错误响应经过统一 handler，不得回传原始异常字符串（当前 `api.py:73`、`knowledge.py:116,143` 已泄漏）

