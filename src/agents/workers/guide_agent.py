from typing import Any

from src.agents.builder import build_worker_agent, get_runtime_agent
from src.core.agent_registry import AgentRuntimeConfig, get_active_config
from src.tools.guide_tools import (
    get_visit_preparation,
    manage_appointment,
    organize_visit_materials,
    query_department_info,
    query_hospital_info,
    recommend_department,
)

GUIDE_SYSTEM_PROMPT = """你是平台医疗的就医辅助助手（GuideAgent）。

你的职责：
1. 科室介绍查询（诊疗范围、常见疾病）
2. 就诊准备（带什么资料、是否需要空腹）
3. 科室推荐（按症状推荐挂号科室）
4. 就医资料整理（把档案、报告、用药、预约汇总成清单）
5. 医院信息查询（等级、地址、门诊时间、电话）
6. 预约系统对接（查询/创建/取消挂号）

## 工具使用规则（必须遵守）

- 问"某科室是看什么的"：调用 query_department_info
- 问"我这个症状该挂什么科"：调用 recommend_department（只做分诊参考；
  胸痛、呼吸困难、意识不清、大出血等急症立即提示急诊/120，不要等推荐结果）
- 问"看病要带什么/要空腹吗"：调用 get_visit_preparation
- 问"帮我整理一下看病要带的资料"：调用 organize_visit_materials（需登录；游客提示登录）
- 问医院等级/地址/门诊时间/电话：调用 query_hospital_info，查不到如实说明，禁止编造地址电话
- 用户明确要挂号/查预约/取消预约：调用 manage_appointment；创建前先与用户确认科室与日期
- 不诊断疾病、不推荐具体药物：涉及诊断与用药的问题请提示转由问诊或药品助手处理

回复格式：
- 推荐科室：xxx（理由：xxx）
- 就诊准备：xxx（分条列出）
- 资料清单：xxx
- 医院信息：xxx
- 预约结果：xxx（预约号/科室/日期）

注意：分诊与预约为辅助信息，最终以医院现场分诊台与挂号系统为准。"""


_AGENT_CODE = "guide"
_DEFAULT_TEMPERATURE = 0.1
_DEFAULT_TOOLS = [
    query_department_info,
    recommend_department,
    get_visit_preparation,
    organize_visit_materials,
    query_hospital_info,
    manage_appointment,
]


def _build(cfg: AgentRuntimeConfig) -> Any:
    return build_worker_agent(
        cfg,
        default_prompt=GUIDE_SYSTEM_PROMPT,
        default_tools=_DEFAULT_TOOLS,
        default_temperature=_DEFAULT_TEMPERATURE,
    )


def create_guide_agent():
    """按当前配置新建一个实例（不写缓存，便于测试/排查）。"""
    return _build(get_active_config(_AGENT_CODE))


def get_guide_agent():
    """取单例：Agent 配置保存/发布后自动重建。"""
    return get_runtime_agent(_AGENT_CODE, _build)
