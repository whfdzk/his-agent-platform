from typing import Any

from src.agents.builder import build_worker_agent, get_runtime_agent
from src.core.agent_registry import AgentRuntimeConfig, get_active_config
from src.tools.health_tools import (
    generate_period_report,
    get_metric_trends,
    manage_health_plan,
    manage_reminder,
    record_vital_metric,
)

HEALTH_SYSTEM_PROMPT = """你是平台医疗的慢病与健康指标管理助手（HealthManageAgent）。

你的职责：
1. 记录血压、血糖、体重（以及心率）
2. 生成指标趋势图
3. 记录与跟踪健康计划
4. 创建测量/用药/复诊提醒
5. 生成周期（周/月）健康报告

## 工具使用规则（必须遵守）

- 用户报出"血压 130/85""血糖 7.2""体重 65 公斤"等数值：调用 record_vital_metric 落库
  （血压必须同时有收缩压和舒张压，缺舒张压时先问清楚再记录）
- 用户问"最近血压怎么样""给我看看趋势图"：调用 get_metric_trends
- 用户要定目标（减重、每日步数、限盐）：调用 manage_health_plan
- 用户要设提醒（每天量血压、晚上吃药）：调用 manage_reminder
- 用户要周报/月报、健康总结：调用 generate_period_report
- 未登录（工具返回游客提示）：如实告知登录后可记录与长期跟踪，不要编造数据
- 记录到的危急值（如血压 ≥180/120、血糖 ≥16.7 或 <2.8）：立即提示线下就诊或急诊

回复格式：
- 记录结果：xxx（数值 + 评估 + 时间）
- 趋势：xxx（贴出工具返回的趋势图与均值/极值）
- 建议：xxx（生活方式提示，不下诊断、不开药）

注意：健康管理提示不能替代医生诊断；有不适或指标持续异常请线下就诊。"""


_AGENT_CODE = "health"
_DEFAULT_TEMPERATURE = 0.1
_DEFAULT_TOOLS = [
    record_vital_metric,
    get_metric_trends,
    manage_health_plan,
    manage_reminder,
    generate_period_report,
]


def _build(cfg: AgentRuntimeConfig) -> Any:
    return build_worker_agent(
        cfg,
        default_prompt=HEALTH_SYSTEM_PROMPT,
        default_tools=_DEFAULT_TOOLS,
        default_temperature=_DEFAULT_TEMPERATURE,
    )


def create_health_agent():
    """按当前配置新建一个实例（不写缓存，便于测试/排查）。"""
    return _build(get_active_config(_AGENT_CODE))


def get_health_agent():
    """取单例：Agent 配置保存/发布后自动重建。"""
    return get_runtime_agent(_AGENT_CODE, _build)
