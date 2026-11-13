from typing import Any

from src.agents.builder import build_worker_agent, get_runtime_agent
from src.agents.tool_registry import tool_map
from src.core.agent_registry import AgentRuntimeConfig, get_active_config

KNOWLEDGE_SYSTEM_PROMPT = """你是平台医疗的统一医学知识服务助手（KnowledgeAgent），是知识管理与检索服务的入口。

你的职责（覆盖范围）：
1. 医学科普知识
2. 常见疾病知识
3. 检验指标知识
4. 药品说明书
5. 医疗指南
6. 就医科室知识
7. 知识来源与版本查询

## 工具使用规则（必须遵守）

- 问题涉及临床指南、药品说明书、医院制度 SOP、文献：调用 search_knowledge_docs
  （doc_type 可传 guideline/drug_instruction/sop/literature/disease/lab_indicator/department 做过滤）
- 问题涉及疾病与症状/药物/科室/检查/食物的关系、多跳推理：调用 search_knowledge_graph
- 复杂问题需要同时参考指南和实体关系（如"糖尿病合并肾功能不全怎么选药"）：调用 search_knowledge_multi
- 用户问"这些说法出自哪里""知识库里有哪些指南/说明书""是不是最新版"：调用 list_knowledge_sources
- 每个问题**只调用一个最合适的工具**，不要为了"更全面"连续调用多个（每次检索都很慢）；只有明确需要同时看指南和图谱时才用 multi
- 至少调用一次工具，不要凭记忆直接回答
- 工具返回"未找到/暂时不可用/降级"时：如实说明知识库暂未覆盖，只用通用医学常识给出方向性建议，禁止编造指南条款、文献标题或数据
- 工具返回里带【依据核查】标注的，说明该部分缺文档支撑，回答时要保留这个提示

回复格式：
- 问题解答：xxx
- 相关知识：xxx
- 参考来源：xxx（引用工具返回的文档名/图谱关系；无来源时写"暂无知识库来源"）
- 延伸阅读：xxx（如有）

注意：知识问答不替代专业医疗建议，如有疑虑请咨询医生。"""


_AGENT_CODE = "knowledge"
_DEFAULT_TEMPERATURE = 0.7
# 代码内置默认工具集：配置缺失或草稿/停用状态下使用
_DEFAULT_TOOL_IDS = (
    "search_knowledge_docs",
    "search_knowledge_graph",
    "search_knowledge_sql",
    "search_knowledge_multi",
    "review_prescription_tool",
    "list_knowledge_sources",
)


def _default_tools() -> list[Any]:
    mapping = tool_map()
    return [mapping[tool_id] for tool_id in _DEFAULT_TOOL_IDS if tool_id in mapping]


def _build(cfg: AgentRuntimeConfig) -> Any:
    """知识问答 Agent。

    工具本身不缓存依赖：真正被调用时才从 contextvar 取当前请求的
    llm/向量库/图谱/DB 会话（见 src/knowledge/deps.py），因此 Agent 可以是单例。
    """
    return build_worker_agent(
        cfg,
        default_prompt=KNOWLEDGE_SYSTEM_PROMPT,
        default_tools=_default_tools(),
        default_temperature=_DEFAULT_TEMPERATURE,
    )


def create_knowledge_agent():
    """按当前配置新建一个实例（不写缓存，便于测试/排查）。"""
    return _build(get_active_config(_AGENT_CODE))


def get_knowledge_agent():
    """取单例：Agent 配置保存/发布后自动重建。"""
    return get_runtime_agent(_AGENT_CODE, _build)
