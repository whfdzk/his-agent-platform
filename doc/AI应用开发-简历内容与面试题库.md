# 智慧医疗 AI Agent 平台 · 简历内容与面试题库（AI 应用开发方向）

> 素材来源：`his-agent-platform` 仓库实际代码（`src/agents`、`src/knowledge`、`src/core`、`src/api`、`alembic`、`test`）与 `doc/` 下的改造总结。
> 文中数字均为代码可核对的配置值/结构量，非虚构指标；「设计上限」类数字已明确标注口径。

---

## 一、项目核心功能（30 秒概括）

一个面向患者与医院内部人员的**医疗对话式 AI 平台**：以 Supervisor 智能体统一入口，按意图调度 7 个垂类 Agent（问诊分诊、报告解读、用药与相互作用、医学知识、慢病健康管理、就医辅助、运营数据），背后接**文档向量库 + 医学知识图谱 + 运营库 NL2SQL** 三条检索通道，并以**确定性的医疗安全引擎**在模型输入/输出两端做拦截与免责兜底；工程侧提供 SSE 流式对话、会话记忆（Redis 短记 + Milvus 长记）、消息幂等与断线续传、全链路 Agent 轨迹审计，以及一套可在线改 Prompt / 换模型 / 调工具 / 发布回滚的 Agent 管理端。

| 能力域 | 交付内容 |
|---|---|
| 对话入口 | FastAPI + SSE 流式（首字节 ping、15s 心跳、断连取消、单轮超时） |
| Agent 编排 | 1 个 Supervisor（ReAct 工具调用）+ 7 个 Worker Agent，共 31 个可配置工具 |
| 问诊链路 | 9 节点 LangGraph 状态机（急症检查→症状抽取→图谱查询→追问/收敛→结论→落库→挂号移交） |
| 知识链路 | 文档 RAG（Milvus + Reranker）、GraphRAG（实体抽取 + NL2Cypher 带重试）、NL2SQL（只读白名单校验）、多通道并行融合 + 引用核查 |
| 记忆 | Redis checkpointer（会话级，1h）+ Milvus 长期记忆（跨会话，按用户） |
| 安全合规 | 25 条危险信号规则（20 输入 + 5 输出）、critical 直接阻断、PHI 脱敏、审计留痕 |
| 可观测 | trace/span 全链路（8 类 span）、Prometheus 指标、Sentry（上报前剥离 PHI） |
| 管理端 | 用户/Agent/Prompt 版本/对话/模型/知识库/安全审计/统计/会员，9 个管理域 |

---

## 二、整体架构与技术选型

### 2.1 分层架构

```
┌──────────────────────────────────────────────────────────────────────┐
│ 接入层  FastAPI + Starlette      /chat/stream(SSE)  /sessions  /admin│
│         LoggingMiddleware(纯 ASGI) + CORS + 限流 + JWT 可选/强制鉴权   │
├──────────────────────────────────────────────────────────────────────┤
│ 编排层  Supervisor Agent (LangChain create_agent + 工具调用)          │
│         ├─ call_inquiry_agent  → Inquiry StateGraph（9 节点）         │
│         ├─ call_report/drug/knowledge/health/guide/operation_agent    │
│         └─ save_memory / search_memory / get_my_profile               │
├──────────────────────────────────────────────────────────────────────┤
│ 能力层  知识链路(doc_rag / graph_rag / nl2sql / fusion / rerank)      │
│         Safety Engine（规则驱动，确定性） · ExecutionBudget（预算）    │
│         Trace（contextvar 贯穿） · persist_queue（攒批落库）           │
├──────────────────────────────────────────────────────────────────────┤
│ 适配层  PostgreSQL(asyncpg) · Redis(业务KV + AsyncRedisSaver)          │
│         Milvus×2(文档/长期记忆) · Neo4j(知识图谱) · RustFS(S3)         │
│         MinerU(文档解析) · DashScope(Rerank/VL) · OpenAI 兼容网关      │
├──────────────────────────────────────────────────────────────────────┤
│ 依赖规则  api → agents → tools → core；core 不反向依赖 modules/infra   │
│         （读库能力由 modules 层通过 set_provider 注入，测试守护）      │
└──────────────────────────────────────────────────────────────────────┘
```

### 2.2 技术选型与理由

| 层次 | 选型 | 选型理由（面试可直接讲） |
|---|---|---|
| Web 框架 | FastAPI + Starlette + uvloop/httptools | 原生 async、SSE 易实现；纯 ASGI 中间件避免 BaseHTTPMiddleware 包裹流式响应的背压/内存问题 |
| Agent 框架 | LangChain `create_agent` + LangGraph `StateGraph` | 开放问答用 ReAct（工具即能力边界），强流程用显式状态机（可测试、可收敛、可中断），两者互补而非二选一 |
| 记忆 | `AsyncRedisSaver` checkpointer + Milvus store | 短期记忆按 thread 隔离、TTL 天然过期；长期记忆按 user 维度向量检索，跨会话复用 |
| 向量库 | Milvus（IVF_FLAT / COSINE / 1024 维） | 需按 doc_type 过滤 + 元数据回传做引用溯源；Qwen3-Embedding-0.6B 输出 1024 维 |
| 图谱 | Neo4j + NL2Cypher（失败带错误回灌重试 2 次） | 症状-疾病-药物-检查的多跳关系用 SQL/向量都不经济 |
| 关系库 | PostgreSQL 16 + asyncpg + Alembic | 会话/消息/问诊/用药/运营多域强一致；13 个迁移版本可回滚 |
| 对象存储 | RustFS（S3 兼容） | 知识库原始文档存储，接口与 MinIO 兼容、可私有化部署 |
| 前端 | pnpm workspaces + React 18 + TS + Vite + Tailwind | patient / admin / shared 三包共享 API 类型，双端独立发布 |
| 可观测 | loguru(JSON) + Prometheus + Sentry + 自研 trace | 预算有限时优先自研 span（能落到业务表、可按 turn 复盘），Prometheus 只暴露 2 个 Gauge |

### 2.3 五个关键设计取舍

1. **强弱分离的编排策略**：开放问题走 ReAct（模型自主选工具），问诊走状态机（`round/phase` 驱动，追问有上限、收敛有阈值）。避免"全用 ReAct 导致问诊不收敛、全用工作流导致泛化差"。
2. **子图与整轮的超时分层**：`WORKER_TIMEOUT=150s < TURN_TIMEOUT=240s`。子图跑满只牺牲子图，Supervisor 仍有时间收尾，不会把已经跑完的问诊结论一起判死。
3. **确定性优先的安全兜底**：医疗场景不把安全交给模型判断——25 条规则命中 critical 直接在调 LLM 前阻断并给急救指引；输出侧再做绝对化诊断/具体剂量/疗效保证的纠正。
4. **依赖倒置的分层**：`core` 只定义配置结构与缓存，读库实现由 `modules` 注入（`set_provider` / `set_writer`），由 `test_layering.py` 守护；避免"底层反向依赖上层"导致的循环导入与不可测。
5. **单例 + 配置指纹的 Agent 治理**：Agent 以模块级单例缓存，缓存失效条件是「运行时配置对象相等」，管理端保存/发布后下一次取用自动重建——做到**不重启进程热更新 Prompt/模型/工具白名单**。

---

## 三、简历正文（可直接使用）

### 3.1 项目背景

医院线上入口长期存在三个断层：患者侧的"症状—科室—报告—用药"咨询分散在多个系统，夜间与基层缺乏即时响应；院内知识（指南、说明书、SOP）以 PDF 沉淀，检索靠人工，无法被对话直接调用；通用大模型直接用于医疗存在**幻觉、越权取数、急症误判**三类风险，无法通过院内合规评审。

项目目标是把"对话"做成可落地的一层：以 Agent 统一入口承接患者咨询，用检索增强把答案锚定到院内知识与图谱，用确定性安全规则兜住急症与合规红线，并让运营侧能在线治理 Prompt/模型/工具，而不依赖发版。系统按 **单机 Docker Compose + Nginx、1 万在线用户**的目标做容量设计与稳定性改造。

### 3.2 个人职责

- **架构与选型**：主导 Supervisor + Worker 的分层 Agent 架构与 LangGraph 问诊状态机设计，确定"开放问答走 ReAct、强流程走状态机"的混合编排策略。
- **核心链路开发**：实现 SSE 流式对话网关、意图路由与 7 个 Worker 工具、会话记忆（Redis + Milvus）、问诊状态机 9 个节点及置信度收敛算法。
- **知识链路建设**：完成文档 RAG（切分/向量化/精排/溯源）、GraphRAG（实体抽取 + NL2Cypher 重试）、NL2SQL（只读与隐私字段白名单）与三通道并行融合 + 引用核查。
- **医疗安全与合规**：设计并实现规则驱动的 Safety Engine（输入/输出双端）、PHI 脱敏、JWT 鉴权与越权修复、审计事件与留存策略。
- **高并发与稳定性**：完成面向 1 万在线的生产化改造（并发信号量、预算控制、批量落库、幂等、会话互斥锁、超时分层、降级开关）。
- **平台化治理**：建设配置化 Agent 注册表与工具注册表、Prompt 版本与发布回滚、Agent 轨迹审计与管理端 9 个域的后端接口。
- **质量保障**：建立 31 个 pytest 测试文件（默认跑确定性单测、集成用例按 marker 隔离）与分层依赖守护测试。

### 3.3 技术栈

`Python 3.13` `FastAPI` `Starlette` `asyncio` `LangChain` `LangGraph` `PostgreSQL/asyncpg` `SQLAlchemy` `Alembic` `Redis`（AsyncRedisSaver / 滑动窗口限流 / 分布式锁）`Milvus` `Neo4j` `RustFS(S3)` `MinerU` `Qwen3-Embedding` `Qwen3-Rerank` `DeepSeek/OpenAI 兼容网关` `Prometheus` `Sentry` `loguru` `pytest` `Docker Compose` `Nginx`；前端 `React 18 + TypeScript + Vite + Tailwind`。

### 3.4 关键实现与可量化成果

**1）SSE 流式对话网关的生产化改造（可用性）**
重构 `/chat/stream`：首字节立即 ping、15s 注释帧心跳、`request.is_disconnected()` 断连即取消 LLM 任务、单轮 240s 硬超时、错误统一为 `{code,message,retryable}` 且堆栈只落日志。配套 Nginx `proxy_buffering off` + `proxy_read_timeout 300s`，解决"首 token 前被代理掐断"与"断连后仍空跑完整链路"两类线上问题。

**2）并发与成本的双层护栏（容量）**
新增全局 LLM 信号量（200/进程，2 副本 400）与 `guarded/guarded_stream` 统一包装 Supervisor、问诊图与 4 个 Worker，排队超 15s 快速失败为"系统繁忙"；叠加 `ExecutionBudget` 单轮预算（12 次 Agent 调用 / 20 次工具调用 / 60k token / 8 条检索结果）与按用户 Redis 并发槽（2 个），把"模型跑飞"从不可控变为可枚举的错误类型。连接层再叠 4000 SSE/进程与 Nginx `limit_conn/limit_req`。

**3）写入放大治理（DB 压力）**
新增进程内 `persist_queue`：50 条或 500ms 攒批、单 consumer 批量 upsert，消息与 `token_usage` 不再"每条/每次 LLM 调用各建一个 session 并 commit"；聊天消息用**有界等待**（0.5s）而非静默丢弃，统计类数据允许丢弃并告警；关闭时 `asyncio.shield` 保证余量 flush。DB 侧 `pool_size=30/max_overflow=20`、`pool_pre_ping`、`pool_recycle=5min`，SSE 端点改为 DB 会话短作用域，长连接不再占连接池。

**4）幂等、互斥与断线续传（一致性）**
客户端 `client_message_id` + Redis `SETNX`（5 分钟窗口）实现消息幂等，重复提交直接重放上次结果；失败路径主动释放幂等键，避免"一次超时被放大成 5 分钟不可用"；同会话 `lock:turn:{thread}:{session}` 互斥锁 TTL 取 `TURN_TIMEOUT+30s`，消除并发 read-modify-write 交叉覆盖问诊状态；SSE 事件带会话内递增 `seq`，配合 `GET /chat/stream/pending?after_seq=` 增量补发与 `conversations.last_seq` 落库，实现断线续传。

**5）三通道检索 + 引用核查（答案可信度）**
文档 RAG：512/64 重叠切分 → 1024 维向量 → 粗检索 20 条 → Reranker 精排 5 条（Reranker 不可用自动回退向量序）→ 带 `doc_name/page/chunk` 溯源生成。GraphRAG：LLM 抽实体 → NL2Cypher → 失败把报错回灌重试 2 次。NL2SQL：只允许 SELECT、禁止 DDL/DML、`patients` 表禁用 `phone/id_card/*`、强制 LIMIT 100、10s 执行超时。三通道 `asyncio.gather(return_exceptions=True)` 并行，单通道失败不拖垮整体；生成后再做一次引用核查（15s 预算，超时按"未核查"返回，不为核查多等一轮），无支撑内容显式标注。

**6）配置化 Agent 治理（研发效率）**
建立 `AgentRuntimeConfig` 运行时配置（Prompt / 模型与温度 / 工具白名单 / 子 Agent 白名单 / 会话压缩阈值 / max_turns / 降级话术 / 发布状态），以「配置对象相等」为单例失效条件实现保存即热更新；`tool_registry` 把散落的 31 个工具收成唯一事实源，管理端目录由运行时工具自动生成（此前手写副本只有 11 条，20 个工具无法配置）；未知工具 id 只告警不抛错，保证脏配置不会让 Agent 建不起来。

**7）医疗安全引擎与可观测（合规）**
25 条危险信号规则分输入/输出两侧：输入侧命中 critical（胸痛、呼吸困难、意识障碍、过敏性休克等）**不调 LLM** 直接给急救指引并阻断本轮；输出侧对绝对化诊断、具体剂量、疗效保证追加免责声明，自伤方法类直接替换。每次审核写 `safety_events` 并挂到 trace 的 safety span。自研 trace 覆盖 8 类 span（api/agent/tool/llm/retrieval/safety/final_response/persistence），记录 `trace_id/span_id/parent`、耗时、token、检索来源与安全结论，采样率可配（`TRACE_SAMPLE_RATE`），留存期分离（轨迹 180 天 / 安全事件 365 天），Sentry `before_send` 用正则剥离身份证与手机号，杜绝 PHI 出网。

**8）问诊状态机的可控收敛（效果）**
9 节点图 + round/phase 驱动入口分发（首轮 `load_context`、追问中 `parse_answer`、澄清后 `extract_symptoms`）；候选疾病在基础置信度上叠加**上下文权重**（既往史 +0.15、长期记忆 +0.10、核心症状被否认 −0.20）；收敛条件为 Top1 ≥ 0.70 或 Top1−Top2 ≥ 0.30，最多 10 轮强制收束并标注"信息不足"。明确回退了"编译期图缓存"（闭包持有请求级 `db_session`，缓存会导致会话串扰），保留 Milvus 客户端进程级复用。

### 3.5 设计亮点与差异化优势

| 对比维度 | 低代码编排平台（Dify/Coze 类） | 传统 HIS 内嵌问答 | 通用 RAG Demo | **本项目** |
|---|---|---|---|---|
| 医疗安全 | 需自行搭规则 | 无生成能力 | 无 | 25 条规则 + 输入前阻断 + 输出改写 + 落库审计 |
| 流程可控性 | 可视化编排，难做状态收敛 | 无 | 单轮问答 | 状态机 + 置信度收敛 + 轮次上限 + 挂号移交 |
| 检索能力 | 以文档向量为主 | 关键词 | 单通道 | 文档/图谱/运营库三通道并行 + 精排 + 引用核查 |
| 治理与迭代 | 平台侧黑盒 | 发版 | 改代码 | Prompt 版本/发布回滚/模型热切换/工具白名单在线改 |
| 可观测 | 平台日志 | 应用日志 | print | 自研 turn 级 trace（含检索来源与安全结论） |
| 并发承载 | 依赖平台配额 | 低并发 | 单机 | 信号量+预算+批量落库+幂等+断线续传，按 1w 在线设计 |
| 身份与越权 | 弱 | 强但封闭 | 无 | 服务端身份为准（token sub），游客/登录双模，不信任请求体 |

**一句话差异化**：把"医疗 AI 的可信度"拆成三层可工程化的东西——**检索要能溯源、结论要能收敛、风险要能阻断**，而不是寄希望于一句更好的 Prompt。

### 3.6 一页简历精简版（建议直接粘贴）

> **智慧医疗 AI Agent 平台｜AI 应用开发 / 后端负责人**
> Python · FastAPI · LangGraph · Milvus · Neo4j · PostgreSQL · Redis
> - 设计 Supervisor + 7 Worker 的分层 Agent 架构与 9 节点问诊状态机，落地"开放问答走 ReAct、强流程走状态机"的混合编排，问诊以 Top1≥0.70/Top1-Top2≥0.30 双阈值收敛并支持最多 10 轮收束。
> - 建设文档 RAG + GraphRAG + NL2SQL 三通道并行检索（20 粗排→5 精排、NL2Cypher 报错回灌重试），叠加引用核查与逐条溯源，把答案从"生成"变成"可核查"。
> - 实现规则驱动 Safety Engine（25 条规则、critical 输入前阻断、输出侧免责与替换）与 PHI 脱敏、审计留存，满足院内合规评审要求。
> - 完成面向 1 万在线的生产化改造：LLM 信号量 + 单轮预算 + 批量落库（50 条/500ms）+ 消息幂等 + 会话互斥 + SSE 心跳/断连取消/断线续传，超时分层为子图 150s / 整轮 240s。
> - 建设配置化 Agent 治理（Prompt 版本、发布回滚、模型热切换、31 个工具统一注册）与自研 turn 级全链路 trace，管理端 9 域在线可运营；配套 31 个 pytest 测试文件与分层依赖守护。

---

## 四、AI 应用开发面试题库（基于本项目）

> 用法：每题给「考察点 / 答题骨架 / 事实依据」。答题骨架按"结论先行 → 取舍 → 代价"三段说，最容易被面试官记住。

### 4.0 开场：90 秒项目自述模板

> "这是一个医疗对话 AI 平台。它要解决的不是'能不能聊'，而是'敢不敢用'。所以我在架构上做了三个决定：
> 第一，编排上强弱分离——开放问题用 ReAct 让模型自己选 31 个工具里的合适能力，问诊这种必须收敛的流程用 LangGraph 状态机，9 个节点、双阈值收敛、最多 10 轮。
> 第二，答案必须可溯源——文档向量、医学图谱、运营库 NL2SQL 三通道并行检索再融合，生成后还做一次引用核查，没有依据的内容显式标注。
> 第三，风险不交给模型——25 条确定性规则，命中急症在调模型之前就阻断并给急救指引，输出侧再做合规改写。
> 工程上我按 1 万在线做了改造：信号量、预算、批量落库、幂等、断线续传，超时做了子图 150s / 整轮 240s 的分层，避免子流程跑满把已完成的结论一起判死。"

---

### 4.1 架构与选型（5 题）

**Q1. 为什么同时用 ReAct 和状态机？只用一种不行吗？**
- 考察点：编排范式理解、边界判断。
- 骨架：ReAct 优势是工具组合泛化（"我头疼还吃了布洛芬"可串两个 Worker），劣势是不可控（可能重复调工具、不收敛、token 不可预估）；状态机优势是流程可测可收敛（问诊必须问够症状才能出结论），劣势是泛化差。所以按"是否需要确定性收敛"切分，Supervisor 层用 ReAct 做意图路由，问诊内部用 `StateGraph`，并通过 `call_inquiry_agent` 单向进入。
- 事实依据：`supervisor_agent.py` 用 `create_agent` + 7 个 `call_*_agent` 工具；`inquiry/graph.py` 用 `StateGraph` + `add_conditional_edges` 做 6 处条件路由。

**Q2. Supervisor 和 Worker 之间怎么传状态？为什么不共享 checkpointer？**
- 骨架：Supervisor 用 `AsyncRedisSaver` 做 thread 级短期记忆（`thread_id = user_id:session_id`）；Worker 不挂 checkpointer（避免多实例写同一 thread 造成状态污染），改为**显式传参**：`UserContext(user_id, session_id, authenticated, patient_id)` 走 LangGraph 的 `context`，工具侧从 `runtime.context` 取，取不到才回退解析 `thread_id`。问诊自己的多轮状态单独存 Redis `inquiry_state:{thread}:{session}`（1h TTL）。
- 代价：状态有两套（对话消息 / 问诊状态），需要靠 `inquiry_active` 标记决定本轮进 Supervisor 还是进问诊图。
- 事实依据：`worker_tools.py::_ctx_of`、`chat.py::_run_inquiry_turn`。

**Q3. 你们为什么回退了"编译好的图缓存"？这是个很反常的决定。**
- 骨架：图节点用闭包绑定 `InquiryDeps`，而 `deps` 里含**按请求注入的 `db_session`**。缓存编译结果会让后续请求复用上一个请求的 session，表现为"会话串扰 / session closed"。所以改为每轮编译，编译开销（毫秒级）相对 LLM 调用（秒级）可忽略；真正值得复用的是**无状态客户端**——Milvus 客户端用 `lru_cache` 进程级复用，避免每条消息新建 gRPC 连接。
- 加分点：能说出"缓存要缓存无请求态的东西"这条原则。
- 事实依据：`inquiry/graph.py::run_inquiry` 注释、`doc/2026-10-24-后端生产化改造总结.md` 第 6 节。

**Q4. 分层依赖是怎么强制的？`core` 为什么不能 import `modules`？**
- 骨架：`api → agents → tools → core` 单向；`core` 需要读库（候选模型链、Agent 配置、落库实现），但读库属于 `modules/infra`。解法是**依赖倒置 + 注入**：`core` 定义 `set_provider()` / `set_writer()` 与数据结构，启动时由 `main.py` 注入 `load_chat_candidates` / `load_agent_configs` / `write_batch`。并用 `test_layering.py` 做静态守护，防止后人加一行 import 就破窗。
- 收益：core 可单测（无外部依赖）、无循环导入、换存储不动核心逻辑。
- 事实依据：`core/model_registry.py`、`core/agent_registry.py`、`core/persist_queue.py` 的头注释；`test/test_layering.py`。

**Q5. 如果要把这个平台改成多租户（医院 A/B 数据隔离），你会怎么改？**
- 骨架：① 身份层 payload 加 `tenant_id/role/scopes`，`get_current_user()` 返回 `AuthContext`；② 中间件注入 `tenant_contextvar`；③ 数据层所有仓储加 `tenant_id` 过滤（或 PG RLS 兜底），长期记忆 namespace 加 tenant 前缀；④ checkpointer 的 `thread_id` 前缀加 tenant，避免跨租户串 thread；⑤ 缓存 key、限流 key、并发槽 key 全部加 tenant 维度；⑥ 迁移分批 + 双写开关。
- 加分点：指出"最容易漏的是缓存 key 和向量库 namespace"，以及"RLS 是兜底不是替代应用层过滤"。

---

### 4.2 Agent 工程化（6 题）

**Q6. Agent 单例缓存怎么做？配置改了怎么生效而不用重启？**
- 骨架：`_supervisor_cache = (cfg, agent)`，`cfg` 是 `@dataclass(frozen=True)`，可直接用 `==` 比较。管理端保存/发布后调 `refresh_agents()` 刷新缓存，下一次 `get_supervisor_agent()` 发现配置对象不等 → 加锁重建。并发建链用 `asyncio.Lock` 双检，避免重复建 checkpointer/索引。
- 坑：锁不能复用同一把——`get_supervisor_checkpointer` 被 `_build_supervisor` 调用，若共用非重入锁会**永久死锁**，所以拆成两把锁。
- 事实依据：`supervisor_agent.py` 的 `_supervisor_lock` / `_checkpointer_lock` 注释。

**Q7. 工具越来越多，怎么避免"配置里的工具"和"代码里的工具"脱节？**
- 骨架：做 `tool_registry` 单一事实源：按分组函数级 import 聚合 31 个工具，`resolve_tools(tool_ids, fallback)` 解析白名单，未知 id **只告警不抛错**（库里可能残留已下线工具名）；管理端目录由运行时工具自动生成，不再手写副本；校验用静态名单（不依赖能否 import），避免"某个分组依赖缺失把正确配置判非法"。
- 收益：工具从"11 条手写目录"变成"31 条自动目录"，且新增工具零改管理端。
- 事实依据：`agents/tool_registry.py` 头注释与 `validate_tool_ids`。

**Q8. Worker 的依赖（LLM、Milvus、DB session）怎么在"Agent 常驻单例"下按请求注入？**
- 骨架：用 `contextvars`。知识类工具不持有依赖，`build_knowledge_deps()` 每请求构造后 `bind_knowledge_deps(deps)`，工具执行时 `current_knowledge_deps()` 取当前请求的依赖。这样 Agent 可以单例复用（省去重复建链），依赖又不会串请求。DB 会话只在需要 NL2SQL 时开（`with_db_session=True`），避免长耗时 Agent 期间占连接、断连触发连接 terminate 噪音。
- 加分点：contextvars 在 asyncio task 中自动继承，SSE 生成器里创建的 task 也能拿到——这是选它而不是"全局字典"的关键原因。
- 事实依据：`knowledge/deps.py`、`worker_tools.py::_invoke_knowledge_worker`。

**Q9. `max_turns` 这个业务参数怎么落到框架上？**
- 骨架：LangGraph 用 `recursion_limit`，一次"模型↔工具"往返约消耗 2 个图步，所以 `recursion_limit = max_turns × 2`，`max_turns=0` 表示不限制走框架默认。业务语义（"最多几轮工具调用"）与框架语义（"最多几个图步"）之间必须显式换算，否则配置值会天然差一倍。
- 事实依据：`agents/builder.py::apply_max_turns`。

**Q10. 长会话怎么控上下文成本？**
- 骨架：两层——① `SummarizationMiddleware`，触发条件为 `tokens ≥ 4000` 或 `messages ≥ 6`，保留最近 6 条，阈值全部走 Agent 配置（可在管理端按 Agent 调，不必改代码）；② 单轮 `TURN_MAX_TOKENS=60000` 预算，超限只降级记录、不丢弃已生成内容。检索侧再叠 `RETRIEVAL_MAX_RESULTS=8` 硬上限防"检索把上下文撑爆"。
- 加分点：区分"历史压缩"与"单轮预算"解决的是不同问题（长期成本 vs 单次跑飞）。

**Q11. Agent 输出不稳定，你怎么定位是 Prompt、模型还是检索的问题？**
- 骨架：trace 里带 `agent_name/agent_version/prompt_version/model_name/model_version`，span 分 8 类（api/agent/tool/llm/retrieval/safety/final_response/persistence），检索 span 记录引擎、返回条数、`top_k` 与 `doc_id/score`（不记正文）。排查路径：先看 retrieval span 有没有召回 → 再看 llm span 的 token/耗时/模型 → 最后按 `prompt_version` 对比行为差异。采样率 `TRACE_SAMPLE_RATE` 可调，高峰降到 0.1。
- 事实依据：`core/tracing.py`、`core/versions.py::agent_meta`。

---

### 4.3 RAG 与知识链路（6 题）

**Q12. 描述一下你的 RAG 全链路，每一环的关键参数是什么？**
- 骨架：解析（MinerU 优先，失败回退 LlamaIndex）→ 切分（512 字符 / 64 重叠，按段落聚合）→ 向量化（1024 维）→ 入库（IVF_FLAT, nlist=128, COSINE，元数据含 doc_name/doc_type/page/chunk）→ 查询改写（口语→术语，10s 预算，失败用原问题）→ 粗检索 20 → Reranker 精排 5 → 组装带溯源上下文 → 生成 → 引用核查（15s 预算）。
- 加分点：每个环节都有"失败退路"，不是理想链路。

**Q13. HyDE 你开了吗？为什么？**
- 骨架：代码里支持，但患者问答默认**关闭**（`use_hyde=False`）。原因：HyDE 要多一次 LLM 调用（+1~3s、+成本），而患者问题多为短症状描述，向量检索 + Reranker 已够；它更适合"问题很短但文档很长很术语化"的科研/文献场景，属于可按 doc_type 灰度开启的能力。
- 加分点：能说"不是所有 RAG 技巧都该默认开"，并给出开启条件。

**Q14. Reranker 挂了怎么办？幻觉检测挂了怎么办？——为什么两者策略不同？**
- 骨架：Reranker 失败**回退向量距离排序**（降级但仍有结果，相关性略降）；引用核查失败/超时**按"未核查"直接返回答案并记日志**，因为核查是增强能力，不能为了核查再等一轮模型、把用户答案拖没。而 NL2SQL 的**安全校验失败必须直接返回空**——这是安全断言，不能 fail-open。
- 加分点：一句话总结"增强能力 fail-open，安全断言 fail-closed"。
- 事实依据：`doc_rag.py::GROUNDING_TIMEOUT`、`reranker.py`、`nl2sql.py::_validate_sql`。

**Q15. NL2SQL 你怎么防它删库、读隐私？**
- 骨架：四道闸——① 必须以 `SELECT` 开头，禁 DDL/DML 关键字；② 涉及 `patients` 表时禁 `phone/id_card/*`（先把 `count(*)` 摘掉再判，避免误杀聚合）；③ 无 `LIMIT` 自动补 `LIMIT 100`，返回行数再截断；④ `asyncio.wait_for` 10s 执行超时 + 失败把 DB 报错回灌重试 2 次。再加角色闸：非内部人员调用直接返回"无权访问"（模型误调也拿不到数据）。
- 坑点：不能写"patients 后面跟敏感列"的顺序型正则，`SELECT phone FROM patients` 可绕过。
- 事实依据：`nl2sql.py` 注释与 `knowledge/tools.py::search_knowledge_sql`。

**Q16. GraphRAG 的 NL2Cypher 你怎么保证正确率？**
- 骨架：两段式——先让 LLM 抽实体（疾病/症状/药物/科室/检查），再基于实体生成 Cypher；执行失败把**数据库报错原文**作为 `error_hint` 回灌重试（最多 2 次）；取回记录按 `GRAPH_RAG_RECORD_LIMIT=20` 且受单轮预算 `clamp_retrieval` 双重截断。风险控制上不做写操作，只读查询。
- 追问预备：Cypher 注入怎么办？→ 生产应加只读账号 + 查询模板白名单，当前是靠只读与超时兜底。

**Q17. 三通道并行融合，怎么处理"某一路返回得很慢"？**
- 骨架：`asyncio.gather(return_exceptions=True)`，单通道异常降级为 `None` 而不影响其他通道；融合时按"文档检索结果 / 图谱检索结果 / 运营数据结果"分段注入，每条证据截断 `FUSION_SNIPPET_LIMIT=1000` 防上下文膨胀；证据不足时明确回"所有检索通道均未找到相关信息"，而不是让模型自由发挥。
- 加分点：如果面试官追问"慢通道要不要超时"，答"要，而且应做 per-channel timeout，当前是靠上游各自超时 + 整轮预算兜底，这是可以继续优化的点"。

---

### 4.4 高并发与稳定性（6 题）

**Q18. 1 万在线，你的并发模型是什么？瓶颈在哪？**
- 骨架：按"在线 1w ≈ 同时活跃对话 5~10%"估并发会话；连接层 4000 SSE/进程 × 2 副本；LLM 层 200/进程（对齐 httpx `max_connections=400`，因为默认 100 会把 LLM 吞吐硬封顶）；DB 层 30+20/进程，双副本 100 < PG `max_connections=200`。瓶颈排序：**上游 LLM 配额 > DB 连接 > 事件循环**，所以先做 LLM 信号量 + 预算，再做批量落库。
- 事实依据：`doc/2026-10-24-后端生产化改造总结.md`、`concurrency.py`、`config.py`。

**Q19. SSE 长连接最常见的三个线上问题是什么？**
- 骨架：① 首 token 前被代理/客户端超时掐断 → 立即下发 `: ping` 首字节 + 15s 心跳（必须小于 Nginx `proxy_read_timeout`）；② 客户端断开后服务端仍在跑完整 LLM 链路 → 循环内 `request.is_disconnected()`，断开即 `task.cancel()`；③ 反向代理缓冲导致"内容都生成了但用户看不到" → `proxy_buffering off` + `X-Accel-Buffering: no` + `chunked_transfer_encoding on`。
- 加分点：还有一个隐性坑——用 `BaseHTTPMiddleware` 包 SSE 会引入背压/内存问题，所以日志中间件重写为**纯 ASGI 中间件**。

**Q20. 超时应该设几层？为什么子图要比整轮短？**
- 骨架：四层——单次 LLM（60s，重试 2 次 ≈ 最坏 180s）、Worker 子图（150s）、整轮（240s）、SSE 心跳（15s）。子图必须明显短于整轮，否则子图跑满会把整轮预算吃光，Supervisor 没有时间收尾，**已经跑完的问诊结论被整轮超时一起丢弃**。这是从线上问题反推出来的参数关系，不是拍脑袋。
- 事实依据：`config.py` 的 `TURN_TIMEOUT/WORKER_TIMEOUT` 注释。

**Q21. 消息重复提交、同会话并发、断线重连，你分别怎么处理？**
- 骨架：① 幂等：`client_message_id` + `SETNX idem:{user}:{cmid}`（5min），重复请求重放上次结果，处理中返回 409；失败路径主动删键。② 并发：`lock:turn:{thread}` 互斥锁，TTL = `TURN_TIMEOUT + 30s`（固定 30s 会在长轮次中途过期，导致补发消息并发跑同一 thread 覆盖 checkpoint）。③ 重连：事件带会话内递增 `seq`，`/stream/pending?after_seq=` 增量补发，`last_seq` 落 `conversations` 表，Redis 丢了也能恢复。
- 加分点：把"幂等键也是资源，要能释放"讲出来。

**Q22. 为什么用批量落库队列而不是直接写？队列满了丢消息怎么办？**
- 骨架：1w 并发下"每条消息/每次 LLM 调用各建 session 并 commit"会以万级 QPS 打爆连接池。方案：单 consumer 攒批（50 条或 500ms）+ 少量长连接批量 upsert + 失败重试 3 次；关闭时 `shield` flush。队列满时分两类：**聊天消息用有界等待 0.5s**（宁可短暂阻塞也不静默丢用户对话），**统计类（token_usage）直接丢弃并告警**（不阻断主链路）。消费异常重新入队，不清空 batch。
- 事实依据：`core/persist_queue.py`（`_QUEUE_MAX=20000`、`ENQUEUE_WAIT_TIMEOUT=0.5`）。

**Q23. 上游模型网关抖动怎么扛？**
- 骨架：自研 `FallbackChatModel` 继承 `ChatOpenAI`，`_agenerate/_astream` 里按候选链（DB 配的 `is_default` 优先 + `priority` 升序，最后兜底 `.env` 主模型）逐个尝试；**流式只在"还没吐出第一个 chunk"时才切换**（已输出再重试会重复正文）。候选链热更新（管理端改完即生效），客户端按 `(name, model, base_url, key, temperature)` 缓存复用。
- 为什么不用 `RunnableWithFallbacks`：`create_agent` 需要 `bind_tools`，而它没有，会把整个 agent 打挂。
- 事实依据：`config.py::FallbackChatModel` 类注释。

---

### 4.5 医疗安全与合规（4 题）

**Q24. 医疗场景为什么不能只靠 Prompt 做安全？**
- 骨架：Prompt 是概率约束，医疗红线要求确定性。所以做三层：① 规则引擎（25 条：胸痛/呼吸困难/意识障碍/卒中/过敏性休克/血压危象/血糖危象/自伤等）命中 critical **在调 LLM 之前**阻断并给急救指引——避免模型说"建议再观察"；② 输出侧规则（绝对化诊断、具体剂量、疗效保证、停药建议、自伤方法）追加免责或直接替换；③ 全量落 `safety_events` 并支持人工复核队列。
- 加分点：讲"前置阻断"的价值——省一次 LLM 调用是次要的，**避免模型给出错误安抚**才是主要的。

**Q25. 你怎么保证日志/监控不泄漏患者隐私？**
- 骨架：① trace 只存结构化字段与脱敏摘要，明确"不写完整病历/身份证/手机号/报告原文"；② 检索 span 只记 `doc_id/score/title`，不记正文；③ 序列化输出用 `mask_phone/mask_id_card`；④ Sentry `before_send` 剥离 `request.data` 与栈帧变量，并用身份证/手机号正则做最后一道 scrub；⑤ 留存期分离（轨迹 180 天 / 安全事件 365 天 / 访问日志 30 天）。
- 事实依据：`main.py::_scrub_sentry_event`、`core/tracing.py`、`utils/masking.py`。

**Q26. 越权（IDOR）你们踩过吗？怎么修的？**
- 骨架：原则是"身份只从 token `sub` 取，绝不信任请求体自报的 `user_id/patient_id`"。`get_current_user_id()` 强制登录、`get_optional_user_id()` 可选登录（游客模式），两者都返回服务端解出的 user_id；聊天里游客 `user_id="anon"`、`thread_id="anon:{session}"`（患者 ID 是纯数字，永不冲突）。token 非法/过期返回 401 而**不静默降级为游客**，否则登录态失效会被误判成游客、丢历史。
- 加分点：说清"可选登录"的语义边界：能聊，但不落库、不能新建会话、不能读写长期记忆。

**Q27. 游客模式为什么值得做？技术上最麻烦的是什么？**
- 骨架：降低咨询门槛（很多患者不愿注册）。麻烦在于**权限要贯穿到底**：`persist=False` 要贯穿消息落库、问诊记录、长期记忆、挂号移交四条路径，任一漏判就会给匿名用户写数据。实现上 `ChatIdentity.persist` 单一开关 + 各链路的 `persist` 参数显式传递，并用实测验证"游客聊天后 `conversations/chat_messages` 仍为 0 行"。

---

### 4.6 可观测与评测（3 题）

**Q28. 你怎么衡量这个 Agent 系统"好不好"？现在缺什么？**
- 骨架：分三层指标——① 系统层：SSE 活跃连接、在途 LLM、P95 首 token 时延、超时率、降级率、429/503 比例；② 链路层：每轮 agent 调用数、工具调用分布、检索命中率与 `retrieval_count`、安全拦截率、token 成本/会话；③ 效果层（当前最弱）：需要 gold set 做 Recall@5 / 引用准确率 / 盲评一致性，把"引用核查不通过率"作为幻觉代理指标。
- 加分点：主动承认缺口（评测体系未建），并给出最小可行方案：200 条标注问题集 + 双人盲评 + 引用核查通过率做回归门禁。

**Q29. trace 表数据量会爆炸，你怎么控？**
- 骨架：采样（`TRACE_SAMPLE_RATE`，未采样不建 span 不落库）+ 落库走批量队列（不阻塞主链路）+ 保留期清理（`TRACE_RETENTION_DAYS=180`）+ 只存结构化摘要不存正文 + 检索来源最多 20 条/次。另有 `(created_at)` 索引迁移支撑按时间清理。

**Q30. 让你重做一遍，哪三件事会不一样？**
- 骨架（示范答案，体现反思）：① 评测先行——先把 gold set 和 Recall@5 建起来，再调 RAG 参数，否则每次优化都是凭感觉；② 状态管理收敛——现在"对话消息在 checkpointer、问诊状态在独立 Redis key"两套状态，应统一到一张 turn 状态机里；③ 安全规则外置——25 条规则目前是代码常量，应像 Prompt 一样进管理端可配置 + 灰度生效。

---

### 4.7 压力与开放题（3 题）

**Q31. 患者说"我胸痛"，模型却回了"可能是焦虑，先观察"，你怎么定位和修？**
- 骨架：先看是"没命中规则"还是"命中但没阻断"——查 trace 的 safety span 与 `safety_events` 是否有 `em_chest_pain`；若未命中，是表述变体（"胸口发闷""压着疼"）导致正则漏召，需补同义词/改用规则+小模型双通道；若命中但输出仍被放行，检查 `SAFETY_BLOCK_ON_CRITICAL` 开关与阻断分支是否被前置条件跳过。修复动作：规则补召回 + 回归用例固化 + 把该 case 加进评测集。
- 这题考的是"能不能按链路分层定位"，而不是"能不能改 Prompt"。

**Q32. 业务方要求"回答更快"，但质量不能掉，你怎么做？**
- 骨架：先量化再优化——拆首 token 时延 vs 总时延。手段：① 首 token 优化（路由模型用小模型/低温度、减少 Supervisor 串行跳数、把"检索"和"路由"并行）；② 减少不必要调用（HyDE 默认关、引用核查 15s 预算、单通道失败不重试整链）；③ 缓存（同一问题 + 同一知识版本的结果缓存，需带版本号失效）；④ 降级路径（DEGRADE_MODE、Worker 降级话术先回再补）。每项都要有"质量不掉的证据"，即跑评测集对比。

**Q33. 这个项目里你觉得最难的技术决策是什么？**
- 示范答案：**超时分层**。看起来只是两个数字，实际是"资源分配 + 失败语义"的设计：子图 150s、整轮 240s，意味着系统明确选择"宁可牺牲一个子流程，也要保住已完成的结论"；再叠加预算（12 次 agent / 20 次工具）与信号量排队 15s 快速失败，才把"模型跑飞"从不可控变成可枚举的错误类型。难点不在写代码，在于先把"什么算失败、失败时保住什么"想清楚。

---

## 五、使用建议

1. **简历投放**：用 3.6 的精简版（5 条），技术栈压成一行；面试时再展开 3.4 的 8 条。
2. **数字口径**：对外只讲可核对的量（7 个 Worker、31 个工具、9 节点、25 条规则、50 条/500ms、4000 SSE/进程、150s/240s）；未压测的容量一律说"按 1 万在线做的设计与上限"，不要说"支撑 1 万在线"。
3. **面试节奏**：4.0 自述 90 秒 → 等对方挑一条追问（大概率是 RAG 或高并发）→ 用"结论—取舍—代价"三段答 → 主动抛一个你知道的不足（如评测体系），比全程自夸可信。
4. **避免踩雷**：不要声称做过"多租户/多角色 RBAC/人工接管坐席"——这些在 `doc/2026-10-24-医疗平台多租户多角色改造开发计划.md` 里还是**路线图**，代码未落地；被追问细节会露。
