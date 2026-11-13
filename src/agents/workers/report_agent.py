from typing import Any

from src.agents.builder import build_worker_agent, get_runtime_agent
from src.core.agent_registry import AgentRuntimeConfig, get_active_config
from src.tools.patient_tools import get_recent_reports

REPORT_SYSTEM_PROMPT = """你是平台医疗的报告解读助手。

你的职责：
1. 解读患者上传的检验报告、影像报告
2. 提取关键异常指标，用通俗语言解释含义
3. 标注需要重点关注的项目
4. 给出初步建议（是否需要复查、就诊等）

## 工具使用规则（必须遵守）

- 用户问"我的报告""上次的血常规""结果怎么样"时，先调用 get_recent_reports 拿结构化指标，再解读
- 工具返回"还没有上传过报告"时，如实告知并引导前往「报告中心」上传，禁止编造指标数值
- 用户直接贴出报告内容时，以用户提供的数值为准，不要调用工具
- 只有文本指标可用，影像描述（CT/超声）缺少原始影像，不要推断影像结论，建议线下阅片

回复格式：
- 报告类型：xxx
- 关键发现：xxx
- 异常指标：xxx（正常范围：xxx，当前值：xxx）
- 建议：xxx

注意：解读结果仅供参考，最终诊断以医生为准。"""


_AGENT_CODE = "report"
_DEFAULT_TEMPERATURE = 0.1
_DEFAULT_TOOLS = [get_recent_reports]


def _build(cfg: AgentRuntimeConfig) -> Any:
    return build_worker_agent(
        cfg,
        default_prompt=REPORT_SYSTEM_PROMPT,
        default_tools=_DEFAULT_TOOLS,
        default_temperature=_DEFAULT_TEMPERATURE,
    )


def create_report_agent():
    """按当前配置新建一个实例（不写缓存，便于测试/排查）。"""
    return _build(get_active_config(_AGENT_CODE))


def get_report_agent():
    """取单例：Agent 配置保存/发布后自动重建。"""
    return get_runtime_agent(_AGENT_CODE, _build)
