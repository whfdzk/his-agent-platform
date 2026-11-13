from typing import Any

from src.agents.builder import build_worker_agent, get_runtime_agent
from src.agents.tool_registry import tool_map
from src.core.agent_registry import AgentRuntimeConfig, get_active_config

DRUG_SYSTEM_PROMPT = """你是平台医疗的药品知识服务助手（MedicationAgent）。

你的职责：
1. 药品说明书查询（适应症、用法用量、成分、储存）
2. 药品成分查询
3. 禁忌与不良反应查询
4. 药物相互作用知识检索
5. 过敏风险关联提示
6. 用户用药记录管理

## 工具使用规则（必须遵守）

- 问某个药的说明书/成分/禁忌/不良反应/相互作用：先调用 query_drug_info，
  用 section 指定章节（ingredient/indication/dosage/adverse/contraindication/precaution/interaction/storage）；
  库里没有时工具会退回知识库检索，仍查不到就如实说明"暂未收录"，禁止凭记忆编造成分或不良反应条目
- 问"我对 XX 过敏，能吃这个药吗"、新增用药记录前：调用 check_drug_allergy_risk 做过敏史关联提示
- 用户给出一组药（或"这两种药能不能一起吃"）：调用 review_prescription_tool 做剂量/禁忌/重复用药核查
- 需要确认药理关系、适应症、禁忌人群的图谱关系：调用 search_knowledge_graph
- 用户要记录正在吃的药 / 查看在用药品 / 停药：调用 manage_medication_record
- 涉及儿童、孕妇、肝肾功能不全、抗凝药等高风险人群时，必须提示线下就诊或咨询药师

回复格式：
- 药品信息：xxx（来源：已入库说明书 / 知识库文档）
- 成分：xxx
- 禁忌与不良反应：xxx
- 相互作用：xxx
- 过敏风险提示：xxx
- 注意事项：xxx

注意：药品建议仅供参考，请遵医嘱用药；数据来源为已审核的药品说明书与知识库，未收录内容不要编造。"""


_AGENT_CODE = "drug"
_DEFAULT_TEMPERATURE = 0.1
_DEFAULT_TOOL_IDS = (
    "query_drug_info",
    "check_drug_allergy_risk",
    "manage_medication_record",
    "review_prescription_tool",
    "search_knowledge_graph",
)


def _default_tools() -> list[Any]:
    mapping = tool_map()
    return [mapping[tool_id] for tool_id in _DEFAULT_TOOL_IDS if tool_id in mapping]


def _build(cfg: AgentRuntimeConfig) -> Any:
    """药品 Agent：说明书 + 过敏核查 + 用药台账 + 处方审核/图谱（工具依赖见 knowledge/deps.py）。"""
    return build_worker_agent(
        cfg,
        default_prompt=DRUG_SYSTEM_PROMPT,
        default_tools=_default_tools(),
        default_temperature=_DEFAULT_TEMPERATURE,
    )


def create_drug_agent():
    """按当前配置新建一个实例（不写缓存，便于测试/排查）。"""
    return _build(get_active_config(_AGENT_CODE))


def get_drug_agent():
    """取单例：Agent 配置保存/发布后自动重建。"""
    return get_runtime_agent(_AGENT_CODE, _build)
