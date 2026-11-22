# tools 目录说明

本目录存放**不参与运行、只服务于验证与排障**的脚本。产品代码全部在 `src/`，不要把这里的东西 import 进业务。

## 目录结构

```
tools/
├── verify/                # 可复跑的验收脚本（PASS/FAIL 计数，退出码 0 表示全绿）
│   ├── _verify_tenant_isolation.py
│   ├── _verify_history_event_type.py
│   ├── _verify_c3_realtime.py
│   └── ...                # 含从根目录迁入的 _verify_band_*.py 等历史验收脚本
├── probes/                # 一次性探针：验证某个假设，跑完即可弃
│   ├── _probe_*.py        # 打接口 / 看模型返回 / 抓中间态
│   ├── _check_*.py        # 查表、查配置、查数据
│   ├── out/               # 探针输出（txt / json），已 gitignore
│   └── assets/            # 探针用的音频素材（wav），已 gitignore
└── README.md
```

配套目录：

- `scripts/ops/` — 可复用的运维脚本（建库、建管理员、清 Redis 登录限流、灌初始数据等）。
- `doc/screenshots/` — 验收截图（按链路前缀命名：`c3_*` 转人工、`c4_*` 临床动作、`c5_*` 管理端审计）。
- `backups/` — 数据库转储与 `.env` 历史备份，已 gitignore。
- `logs/` — 运行日志，已 gitignore。
- `_msdata/` — ModelScope 数据集缓存（约 610MB），路径被 `scripts/kb/` 硬编码引用，**不要移动**，已 gitignore。

## 运行方式

脚本依赖项目根的 `src` 包，必须让项目根位于 `sys.path`。两种跑法等价：

```powershell
# 方式一：以模块方式运行（推荐，cwd 必须是项目根）
& "D:\conda_envs\langchain1.2\python.exe" -m tools.probes._probe_drug_path

# 方式二：显式把项目根加进 PYTHONPATH 后按路径运行
$env:PYTHONPATH = "D:\github_xiangmu\his-agent-platform-main\his-agent-platform-main"
& "D:\conda_envs\langchain1.2\python.exe" tools\probes\_probe_drug_path.py
```

`tools/verify/` 下的脚本已自带 `sys.path` 注入（项目根 = `parents[2]`），可直接按路径运行：

```powershell
& "D:\conda_envs\langchain1.2\python.exe" tools\verify\_verify_tenant_isolation.py
```

## 注意

- 部分验收脚本会**写真实库**（建临时租户、造工单、改职称），跑完自行清理；详见各脚本头部注释与 `doc/2026-11-20-多租户多角色路线图落地差距审查.md`。
- 登录相关脚本跑之前先清限流键，否则 5 次/5 分钟后会被 429 挡住：`scripts/ops/_reset_login_rate.py`。
- 探针脚本是历史现场记录，参数（科室、医生、患者 id）可能已与当前数据不一致，复用前先读代码。
