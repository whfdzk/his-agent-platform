# HIS Agent 平台 · 后台管理（Admin）现状检查与分阶段实现方案

## 一、现状检查结论

### 已有能力（本次复用，未重复建设）
| 模块 | 后端 | 前端页面 |
|---|---|---|
| 管理端登录/守卫 | `/api/v1/admin/auth`（`admin_auth.py`，JWT role=admin）+ `deps.get_current_admin` | AdminLoginPage + RequireAdmin |
| 模型管理 | `/api/v1/model-configs`（含故障转移链热更新） | ModelConfigsPage |
| 知识库管理 | `/api/v1/knowledge/docs`（上传/删除/反馈统计/通知） | KnowledgePage |
| 风险审计 | `/api/v1/safety`（安全事件、人工复核、规则） | SafetyPage |
| Agent 轨迹 | `/api/v1/observability`（traces/spans/留存清理） | TracesPage |
| Token 明细 | `/api/v1/stats/tokens` | TokenUsagePage |

### 缺口（本次补齐）
用户管理、Agent 管理、Prompt 版本管理、对话管理与反馈收集、系统配置、统计总览（用户量/调用量）、会员管理。

## 二、模块职责与依赖关系

```
                    ┌─────────────── 管理端统一守卫 get_current_admin ───────────────┐
                    │                                                              │
  用户管理 ──┐       │  Agent 管理 ── 引用 → Prompt 管理（业务键 supervisor.system…） │
  (patients) │       │  (agents)        引用 → 模型管理（config.model 覆盖）          │
             │       │                                                              │
  对话管理 ──┼─只读── │  系统配置 ── 供运行时读取开关/参数（chat、safety、member 分组） │
  (conversations/    │  (system_configs)                                            │
   chat_messages)    │                                                              │
             │       │  风险审计 / 轨迹 ── 数据源：AgentTrace/SafetyEvent（运行时埋点）│
  知识库 ────┘       │                                                              │
                    │  数据统计（P1）── 聚合：patients / conversations / token_usage │
                    │  会员管理（P2）── 套餐→订单→订阅；用户管理提供 user_id 来源     │
                    └──────────────────────────────────────────────────────────────┘
```

依赖要点：
- **Prompt → Agent**：Agent 元数据通过约定业务键（如 `supervisor.system`）引用 Prompt 激活版本，运行时按 key 取内容。
- **模型 → Agent/对话**：对话调用的 LLM 走 model_configs 故障转移链，Agent 可通过 config 覆盖。
- **系统配置 → 全模块**：开关/参数/字典统一存储（param/switch/dict），运行时可读，前端按类型渲染。
- **会员 → 用户**：订单与订阅以 `patients.id` 为外键来源；套餐删除时有订单引用则只下架，保证订单快照可追溯。
- **审计（安全/轨迹）只读**：管理端不篡改埋点数据，仅 SafetyEvent 支持人工复核动作。

## 三、本次实现明细（按优先级分阶段）

### 阶段一 P0（已实现）
| 模块 | 后端路由 | 数据表/字段 | 前端页面 |
|---|---|---|---|
| 用户管理 | `/api/v1/admin/users`（列表搜索/详情/资料编辑/启用禁用） | `patients` + 新增 `enabled` 列；登录侧联动拒登 | UsersPage |
| Agent 管理 | `/api/v1/admin/agents`（CRUD/发布/停用） | `agents`（code 唯一、状态 draft/published/disabled） | AgentsPage |
| Prompt 管理 | `/api/v1/admin/prompts`（CRUD/版本列表/新建版本/激活回滚） | `prompts` + `prompt_versions`（版本不可变，激活=切指针） | PromptsPage（版本抽屉） |
| 对话管理 | `/api/v1/admin/conversations`（列表/消息明细/关闭会话/反馈列表） | 复用 `conversations/chat_messages` + `knowledge_feedback` | ConversationsPage（消息抽屉+反馈 Tab） |
| 系统配置 | `/api/v1/admin/system-configs`（CRUD，key 唯一） | `system_configs`（param/switch/dict） | SystemConfigPage（开关/字典控件） |
| 模型/知识库/风险审计 | 已有 | 已有 | 已有，纳入新导航 |

### 阶段二 P1（已实现）
- 统计总览 `/api/v1/admin/stats/overview`：注册用户（含启用数）、累计调用、累计 Token、会话/消息量 + 近 N 天使用趋势与新增用户趋势。
- DashboardPage 指标卡升级为上述口径；Token 明细沿用 TokenUsagePage。

### 阶段三 P2（已实现）
- 会员域三表：`membership_plans` / `membership_orders` / `user_memberships`。
- 路由 `/api/v1/admin/memberships`：套餐 CRUD（有订单引用自动下架）、订单列表与人工核销（核销即开通/顺延会员）、订阅列表。
- MembersPage：套餐卡片 / 订单表格 / 订阅表格三 Tab + 核销弹窗。

### 交付物清单
- 模型：`src/modules/platform/model.py`、`patients.enabled`
- 路由：`src/api/routers/admin_users|admin_agents|admin_prompts|admin_conversations|admin_system|admin_stats|admin_membership.py`，已在 `src/main.py` 挂载（全部带 admin 守卫）
- 迁移：`alembic/versions/d8a2f5c9e317_add_admin_platform_tables.py`（链上唯一 head）
- 前端：`front/shared` 新增 adminApi 类型与封装；`front/admin` 新增 6 个页面 + 深色渐变侧边栏分组导航（对齐视觉稿）

## 四、验证结果
- 后端：新增模块/路由/模型导入自检通过；`patients.enabled` 存在；迁移链唯一 head。
- 前端：`npm run build -w admin`（tsc + vite）构建通过。
- 落库：执行 `alembic upgrade head` 应用新表（首次启用用户封禁等能力前需执行）。

## 五、后续扩展建议
1. Prompt 运行时加载：supervisor/worker 启动时按 `prompts.key` 拉激活版本并热刷新。
2. Agent config 生效链：`agents.config` 覆盖默认模型/工具，运行时与 model_registry 对接。
3. 会员支付对接：`membership_orders.status=pending` 预留支付回调；核销逻辑可平移为回调处理。
4. 权限细分：admins.role 已预留 viewer/operator，可在 `get_current_admin` 基础上加角色依赖。
5. 审计增强：管理端操作日志（谁改了哪个 Prompt/配置）可复用 prompt_versions.created_by 模式扩展。
