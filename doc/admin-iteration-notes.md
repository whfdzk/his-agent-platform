# 后台管理三项需求：实现范围 / 验收标准 / 交互效果

## 需求 1：所有后台页面登录后方可访问

### 实现范围

| 层次 | 实现 |
|---|---|
| 路由层 | `front/admin/src/router.tsx`：全部管理页挂在 `RequireAdmin` 之下，仅 `/login` 公开 |
| 守卫 | `RequireAdmin.tsx`：`ready=false` 时不渲染不跳转（避免首帧误跳）；`token` 或 `admin` 任一缺失 → `<Navigate to="/login" state={{from}}>` |
| 会话 | `store/adminSession.tsx`：启动时从 `localStorage` 恢复后调 `GET /admin/auth/me` 做**服务端校验**；仅 401/403 才清态（网络异常不踢人，避免后端抖动误登出） |
| 全局失效 | 监听 `@his/shared` 在任意接口 401 时派发的 `his:unauthorized` 事件 → 清内存态 → 守卫立即重定向 |
| 登录页 | `AdminLoginPage.tsx`：已登录访问 `/login` 直接回跳；登录成功后回跳来源页（只接受站内相对路径，防开放重定向） |
| 后端边界 | 全部 `/api/v1/admin/**` 路由挂 `Depends(get_current_admin)`：无 token 401、患者 token 403 |

### 验收标准

1. 未登录直接访问 `/`、`/users`、`/agents`、`/prompts`、`/conversations`、`/system`、`/members`、`/knowledge`、`/models`、`/safety`、`/traces`、`/tokens` 任一 URL → 自动跳 `/login`。
2. 登录后回到最初访问的页面（含 query），不是固定跳首页。
3. 手动删除 `localStorage` 中 `his_admin_token` 后刷新 → 跳登录页。
4. token 过期后任意一次接口请求失败 → 自动登出并跳登录页，不出现"空数据但仍在页面"的状态。
5. 已登录再访问 `/login` → 直接进入后台。
6. `curl` 无 token 请求 `/api/v1/admin/users` 返回 401；用患者 token 请求返回 403。

### 预期交互

- 未登录访问后台秒级跳转到登录页，登录后回到原页面；
- 会话过期时页面自动回到登录页，无需手动刷新；
- 退出登录后立即回到登录页，浏览器回退也无法看到受保护内容（守卫会再次拦截）。

---

## 需求 2：用户管理查看关联患者详细信息

### 实现范围

- 后端 `GET /api/v1/admin/users/{id}` 升级为**聚合详情**（只读）：
  - `user`：基本资料（姓名、性别、年龄、血型、身份证/手机号掩码、过敏史、既往病史、账号状态、注册状态、会话/预约计数）；
  - `profile`：健康档案（出生日期、身高体重腰围、婚育职业城市、吸烟饮酒、慢性病、手术史、家族史、紧急联系人，电话同样掩码）；
  - `recent_conversations`：最近 5 条会话（标题、状态、最后活跃时间）；
  - `recent_appointments`：最近 5 条预约（科室、医生、日期时段、状态、预约号）；
  - `membership`：会员套餐与到期时间（无则 null）。
- 前端 `UsersPage` 新增**详情抽屉**：列表点姓名或"详情"按钮打开；分区渲染账号状态标签、基本资料、健康档案、最近会话、最近预约；底部"编辑资料"一键转编辑弹窗。

### 验收标准

1. 列表点击任一用户姓名/详情 → 右侧抽屉打开并显示该用户全部信息。
2. 无健康档案的用户显示"尚未完善健康档案"，不报错。
3. 手机号、身份证号、紧急联系人电话均为掩码展示（PHI 不外泄）。
4. 会话/预约为空时显示"暂无记录"。
5. 详情接口不存在用户时返回 404；未登录 401。

### 预期交互

- 抽屉从右侧滑出，不打断列表筛选状态；关闭后仍停留在原页码；
- 详情页只读，修改资料仍走原有编辑弹窗，避免误改。

---

## 需求 3：修复 Agent 管理新建 Agent 报错

### 根因（两处，均已修复）

1. **数据库迁移未执行**：本地库停在 `b4e8f2a61c93`，`agents` 等表与 `patients.enabled` 列不存在 → 创建 Agent 直接 `relation "agents" does not exist`（500），用户列表同样报错。
   处理：`alembic upgrade head` 已执行到 `d8a2f5c9e317`；`alembic/env.py` 补上 `src.modules.platform.model` 导入，避免后续 autogenerate 把这些表当成多余表。
2. **异步会话懒加载**：`agents`/`prompts`/`system_configs` 的 `updated_at` 由数据库 `onupdate` 生成，UPDATE（发布、停用、激活版本、修改配置）后该字段处于过期态，序列化时同步触发 IO → `MissingGreenlet`（500）。
   处理：写操作后 `await db.refresh(entity)` 回读；`create_prompt` 带首版内容时同理。

### 附带增强

- 唯一编码并发冲突由 500 改为**明确的 409**（`IntegrityError` → 业务异常）；
- 新增 `DELETE /api/v1/admin/agents/{id}`：已发布需先停用再删除，前端卡片增加删除按钮（便于清理误建数据）。

### 验收标准

1. 填写编码 + 名称即可创建成功，返回 200 且列表立即出现新 Agent（状态"草稿"）。
2. 重复编码返回 409 与中文提示，不出现 500。
3. 发布/停用/删除均返回 200，页面状态徽标同步更新。
4. 服务端日志无 `MissingGreenlet` / `UndefinedTable`。

### 预期交互

- 创建弹窗保存后自动关闭并刷新列表；
- 编码非法（大写/中文/符号开头）时前端即时提示，不发起请求；
- 已发布 Agent 点删除提示"需先停用"，停用后再删成功。

---

## 其他问题排查与修复

| 问题 | 处理 |
|---|---|
| `patients.enabled` 缺失导致用户列表/登录校验异常 | 迁移落库，列默认 `true`；患者登录新增禁用拦截（403 + 中文提示） |
| 发布/停用 Agent 后 `updated_at` 懒加载报错 | 写后 `refresh` |
| 创建带首版内容的 Prompt 报 500 | 写后 `refresh` |
| 修改系统配置报 500 | 写后 `refresh` |
| Prompt 版本抽屉回滚后"当前激活"徽标不同步 | 抽屉内维护本地激活版本，激活/新建后同步刷新列表 |
| `hover:bg-white/8` 非标准 Tailwind 类名不生效 | 改为 `bg-white/10` |
| 用户列表分页/筛选、会话反馈分页缺少总数展示 | 已有 `total` 展示与分页控件（确认可用） |
| Agent 无法删除（此前无接口） | 新增删除接口 + 前端入口 |
| 管理端模块缺少回归测试 | 新增 `test/test_admin_platform.py`：路由守卫、掩码、配置序列化、Agent 入参默认值 |

## 验证记录

- `alembic current` → `d8a2f5c9e317`；平台域 7 张表 + `patients.enabled` 已存在。
- 管理端接口冒烟 24 项全通过（401/403 边界、Agent 增删改发、用户详情聚合、配置/Prompt 增删改、统计/会员列表）。
- `pytest test/test_admin_platform.py test/test_admin_access.py` → 22 passed；可采集的确定性单测 238 passed。
- `npm run build -w admin`（tsc + vite）通过。

> 环境说明：本机 miniconda 环境缺少 `pymilvus`、`langgraph.checkpoint.redis` 等既有可选依赖，`src.main` 全量启动与部分测试文件的收集会失败，与本轮改动无关。
