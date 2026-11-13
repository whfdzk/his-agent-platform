from typing import Any

from src.agents.builder import build_worker_agent, get_runtime_agent
from src.agents.tool_registry import tool_map
from src.core.agent_registry import AgentRuntimeConfig, get_active_config

OPERATION_SYSTEM_PROMPT = """你是医疗平台的运营助手。
你的职责：
1. 根据运营人员的自然语言问题，查询统计数据库中的运营数据
2. 生成数据报表和趋势分析
3. 辅助医院管理层做运营决策

## 工具使用规则

- 运营数据查询必须调用 search_knowledge_sql，禁止凭印象编造数字
- 工具返回"仅对内部运营人员开放"时，如实告知当前账号无权查询，不要猜测数据
- 工具返回"查询失败"时，说明暂时查不到，建议稍后重试

重要安全规则：
- 只允许查询聚合统计数据，严禁返回患者个人信息
- 所有查询结果需脱敏处理
- 如果问题涉及患者隐私，拒绝回答并说明原因

回复格式：
- 数据摘要：xxx
- 关键指标：xxx
- 趋势分析：xxx
- 决策建议：xxx

注意：数据查询结果仅供内部运营参考。"""


_AGENT_CODE = "operation"
_DEFAULT_TEMPERATURE = 0.7
_DEFAULT_TOOL_IDS = ("search_knowledge_sql",)


def _default_tools() -> list[Any]:
    mapping = tool_map()
    return [mapping[tool_id] for tool_id in _DEFAULT_TOOL_IDS if tool_id in mapping]


def _build(cfg: AgentRuntimeConfig) -> Any:
    """运营 Agent：NL2SQL 查询（患者角色调用会被工具层直接拒绝）。"""
    return build_worker_agent(
        cfg,
        default_prompt=OPERATION_SYSTEM_PROMPT,
        default_tools=_default_tools(),
        default_temperature=_DEFAULT_TEMPERATURE,
    )


def create_operation_agent():
    """按当前配置新建一个实例（不写缓存，便于测试/排查）。"""
    return _build(get_active_config(_AGENT_CODE))


def get_operation_agent():
    """取单例：Agent 配置保存/发布后自动重建。"""
    return get_runtime_agent(_AGENT_CODE, _build)
