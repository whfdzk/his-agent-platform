# src/agents/tool_registry.py
#
# 工具注册表：把散落在 src/tools、src/knowledge/tools、src/agents/worker_tools 里的
# 工具按 name 收成一张表，供「Agent 配置里选哪些工具」与「管理端工具目录」共用。
#
# 为什么要有这一层：
# - 原先 admin_agents.AGENT_TOOL_CATALOG 是手写的第三份副本（11 条），
#   而运行时实际有 31 个工具，前端根本配置不到另外 20 个。
# - worker 里到处是 `tools_by_name = {t.name: t for t in build_knowledge_tools()}`，
#   每次创建 Agent 都重建一次映射。收成注册表后只有一份。
#
# 依赖方向：本模块只被 agents / api 使用；内部一律函数级 import，
# 避免 worker_tools → workers → tool_registry → worker_tools 的循环导入。

from __future__ import annotations

from typing import Any, Iterable

from loguru import logger

# 工具分类（只影响管理端分组展示，不影响运行时）
TOOL_CATEGORIES: dict[str, str] = {
    "call_inquiry_agent": "Agent 调度",
    "call_report_agent": "Agent 调度",
    "call_drug_agent": "Agent 调度",
    "call_knowledge_agent": "Agent 调度",
    "call_operation_agent": "Agent 调度",
    "call_health_agent": "Agent 调度",
    "call_guide_agent": "Agent 调度",
    "save_memory": "记忆",
    "search_memory": "记忆",
    "get_my_profile": "患者数据",
    "get_recent_reports": "患者数据",
    "query_drug_info": "用药",
    "check_drug_allergy_risk": "用药",
    "manage_medication_record": "用药",
    "record_vital_metric": "健康管理",
    "get_metric_trends": "健康管理",
    "manage_health_plan": "健康管理",
    "manage_reminder": "健康管理",
    "generate_period_report": "健康管理",
    "query_department_info": "就医辅助",
    "recommend_department": "就医辅助",
    "get_visit_preparation": "就医辅助",
    "organize_visit_materials": "就医辅助",
    "query_hospital_info": "就医辅助",
    "manage_appointment": "就医辅助",
    "search_knowledge_docs": "知识检索",
    "search_knowledge_graph": "知识检索",
    "search_knowledge_sql": "知识检索",
    "search_knowledge_multi": "知识检索",
    "search_knowledge_agentic": "知识检索",
    "review_prescription_tool": "知识检索",
    "list_knowledge_sources": "知识检索",
    "request_human_handoff": "人工接管",
}

# Supervisor 可调度的 worker：code → 调度工具名
WORKER_TOOL_IDS: dict[str, str] = {
    "inquiry": "call_inquiry_agent",
    "report": "call_report_agent",
    "drug": "call_drug_agent",
    "knowledge": "call_knowledge_agent",
    "operation": "call_operation_agent",
    "health": "call_health_agent",
    "guide": "call_guide_agent",
}

# 内置「技能」分组：本质是工具集合的别名，便于管理端按业务能力批量选择
SKILL_CATALOG: list[dict[str, Any]] = [
    {"id": "inquiry", "name": "症状问诊与分诊", "description": "收集症状信息并提供就诊分诊建议",
     "tool_ids": ["call_inquiry_agent"]},
    {"id": "medication_safety", "name": "用药安全", "description": "药物咨询、相互作用识别与处方审查",
     "tool_ids": ["call_drug_agent", "query_drug_info", "check_drug_allergy_risk",
                  "manage_medication_record", "review_prescription_tool"]},
    {"id": "medical_knowledge", "name": "医学知识检索", "description": "基于医学文档库与知识图谱回答专业问题",
     "tool_ids": ["call_knowledge_agent", "search_knowledge_docs", "search_knowledge_graph",
                  "search_knowledge_multi", "search_knowledge_agentic", "list_knowledge_sources"]},
    {"id": "report_interpretation", "name": "检查报告解读", "description": "结合结构化指标解释检查报告",
     "tool_ids": ["call_report_agent", "get_recent_reports"]},
    {"id": "health_management", "name": "健康管理", "description": "管理健康指标、趋势和提醒",
     "tool_ids": ["call_health_agent", "record_vital_metric", "get_metric_trends",
                  "manage_health_plan", "manage_reminder", "generate_period_report"]},
    {"id": "care_guidance", "name": "就医流程辅助", "description": "提供科室、医院与就医流程信息",
     "tool_ids": ["call_guide_agent", "query_department_info", "recommend_department",
                  "get_visit_preparation", "organize_visit_materials", "query_hospital_info",
                  "manage_appointment"]},
    {"id": "operations_analytics", "name": "医院运营分析", "description": "面向运营人员查询统计数据",
     "tool_ids": ["call_operation_agent", "search_knowledge_sql"]},
    {"id": "patient_memory", "name": "患者记忆与档案", "description": "长期记忆读写与患者档案查询",
     "tool_ids": ["save_memory", "search_memory", "get_my_profile"]},
]

_tool_cache: dict[str, Any] | None = None


def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _patient_tools() -> list[Any]:
    from src.tools.patient_tools import get_my_profile, get_recent_reports

    return [get_my_profile, get_recent_reports]


def _medication_tools() -> list[Any]:
    from src.tools.medication_tools import (
        check_drug_allergy_risk,
        manage_medication_record,
        query_drug_info,
    )

    return [query_drug_info, check_drug_allergy_risk, manage_medication_record]


def _health_tools() -> list[Any]:
    from src.tools.health_tools import (
        generate_period_report,
        get_metric_trends,
        manage_health_plan,
        manage_reminder,
        record_vital_metric,
    )

    return [
        record_vital_metric, get_metric_trends, manage_health_plan,
        manage_reminder, generate_period_report,
    ]


def _guide_tools() -> list[Any]:
    from src.tools.guide_tools import (
        get_visit_preparation,
        manage_appointment,
        organize_visit_materials,
        query_department_info,
        query_hospital_info,
        recommend_department,
    )

    return [
        query_department_info, recommend_department, get_visit_preparation,
        organize_visit_materials, query_hospital_info, manage_appointment,
    ]


def _memory_tools() -> list[Any]:
    from src.tools.store_tools import save_memory, search_memory

    return [save_memory, search_memory]


def _knowledge_tools() -> list[Any]:
    from src.knowledge.tools import build_knowledge_tools

    return build_knowledge_tools()


def _worker_tools() -> list[Any]:
    from src.agents.worker_tools import WORKER_TOOLS

    return list(WORKER_TOOLS)


def _handoff_tools() -> list[Any]:
    from src.tools.handoff_tools import request_human_handoff

    return [request_human_handoff]


def _skill_tools() -> list[Any]:
    from src.tools.skill_tools import list_skills, load_skill

    return [list_skills, load_skill]


_TOOL_GROUPS = (
    ("患者数据", _patient_tools),
    ("用药", _medication_tools),
    ("健康管理", _health_tools),
    ("就医辅助", _guide_tools),
    ("人工接管", _handoff_tools),
    ("技能 SOP", _skill_tools),
    ("记忆", _memory_tools),
    ("知识检索", _knowledge_tools),
    ("Agent 调度", _worker_tools),
)


def _build_tool_cache() -> dict[str, Any]:
    """构造 name → tool 对象映射。工具本身无状态，可安全复用（依赖走 contextvar）。

    按分组分别 import：某个分组的依赖缺失（如本地没装 neo4j）只丢那一组，
    不会让整个工具目录变空、进而让管理端保存配置全部失败。
    """
    mapping: dict[str, Any] = {}
    for label, loader in _TOOL_GROUPS:
        try:
            for tool in loader():
                mapping[tool.name] = tool
        except Exception as e:
            logger.warning(f"工具分组「{label}」加载失败，该组工具暂不可用: {type(e).__name__}: {e}")
    return mapping


def tool_map() -> dict[str, Any]:
    """name → tool 对象（首次访问时构造，之后复用）。"""
    global _tool_cache
    if _tool_cache is None:
        _tool_cache = _build_tool_cache()
    return _tool_cache


def reset_tool_cache() -> None:
    """仅测试用：丢弃已构造的映射。"""
    global _tool_cache
    _tool_cache = None


def known_tool_ids() -> list[str]:
    try:
        return sorted(tool_map().keys())
    except Exception as e:  # 工具模块 import 失败不应让管理端目录 500
        logger.warning(f"工具注册表加载失败: {type(e).__name__}: {e}")
        return []


def tool_catalog() -> list[dict[str, Any]]:
    """管理端工具目录：由运行时工具自动生成，避免手写副本再次脱节。"""
    items: list[dict[str, Any]] = []
    for name, tool in sorted(tool_map().items()):
        items.append({
            "id": name,
            "name": _display_name(name),
            "description": _first_line(getattr(tool, "description", "")) or name,
            "category": TOOL_CATEGORIES.get(name, "其他"),
        })
    return items


_DISPLAY_NAMES: dict[str, str] = {
    "call_inquiry_agent": "智慧问诊",
    "call_report_agent": "报告解读",
    "call_drug_agent": "用药咨询",
    "call_knowledge_agent": "医学知识问答",
    "call_operation_agent": "运营数据查询",
    "call_health_agent": "健康管理",
    "call_guide_agent": "就医辅助",
    "save_memory": "保存对话记忆",
    "search_memory": "检索对话记忆",
    "get_my_profile": "查询患者档案",
    "get_recent_reports": "查询近期报告",
    "query_drug_info": "药品说明书查询",
    "check_drug_allergy_risk": "过敏风险核查",
    "manage_medication_record": "用药记录管理",
    "record_vital_metric": "记录健康指标",
    "get_metric_trends": "指标趋势查询",
    "manage_health_plan": "健康计划管理",
    "manage_reminder": "提醒管理",
    "generate_period_report": "周期健康报告",
    "query_department_info": "科室信息查询",
    "recommend_department": "科室推荐",
    "get_visit_preparation": "就诊准备",
    "organize_visit_materials": "就医资料整理",
    "query_hospital_info": "医院信息查询",
    "manage_appointment": "预约挂号管理",
    "search_knowledge_docs": "知识文档检索",
    "search_knowledge_graph": "知识图谱检索",
    "search_knowledge_sql": "运营数据 NL2SQL",
    "search_knowledge_multi": "多通道融合检索",
    "search_knowledge_agentic": "自主多轮检索",
    "review_prescription_tool": "处方审查",
    "list_knowledge_sources": "知识来源列表",
    "request_human_handoff": "转人工（建工单）",
}


def _display_name(tool_id: str) -> str:
    return _DISPLAY_NAMES.get(tool_id, tool_id)


def skill_catalog() -> list[dict[str, Any]]:
    return [dict(item) for item in SKILL_CATALOG]


def resolve_tools(
    tool_ids: Iterable[str] | None,
    fallback: list[Any] | None = None,
) -> list[Any]:
    """按 id 列表取工具对象；列表为空时用调用方给的代码内置默认。

    未知 id 只告警不抛错：库里可能残留已下线的工具名，
    这时宁可少一个工具，也不该让整个 Agent 建不起来。
    """
    ids = [str(i).strip() for i in (tool_ids or []) if str(i).strip()]
    if not ids:
        return list(fallback or [])

    mapping = tool_map()
    resolved: list[Any] = []
    missing: list[str] = []
    for tool_id in ids:
        tool = mapping.get(tool_id)
        if tool is None:
            missing.append(tool_id)
            continue
        resolved.append(tool)
    if missing:
        logger.warning(f"Agent 配置引用了不存在的工具，已忽略: {missing}")
    return resolved or list(fallback or [])


def resolve_worker_tools(
    sub_agent_codes: Iterable[str] | None,
    fallback: list[Any] | None = None,
) -> list[Any]:
    """按 worker code 取「调度该 worker」的工具对象（Supervisor 专用）。"""
    codes = [str(c).strip() for c in (sub_agent_codes or []) if str(c).strip()]
    if not codes:
        return list(fallback or [])

    mapping = tool_map()
    resolved: list[Any] = []
    missing: list[str] = []
    for code in codes:
        tool_id = WORKER_TOOL_IDS.get(code)
        tool = mapping.get(tool_id) if tool_id else None
        if tool is None:
            missing.append(code)
            continue
        resolved.append(tool)
    if missing:
        logger.warning(f"Supervisor 配置引用了未知的子 Agent，已忽略: {missing}")
    return resolved or list(fallback or [])


def validate_tool_ids(tool_ids: Iterable[str]) -> list[str]:
    """返回无法识别的工具 id（供管理端保存前校验）。

    校验用静态名单（TOOL_CATEGORIES 覆盖全部工具），不依赖运行时工具能否 import：
    某个分组依赖缺失时不能反过来把正确的配置判成非法。
    """
    known = set(TOOL_CATEGORIES) | set(WORKER_TOOL_IDS.values())
    return sorted({str(i) for i in tool_ids if str(i) not in known})


def validate_sub_agent_codes(codes: Iterable[str]) -> list[str]:
    return sorted({str(c) for c in codes if str(c) not in WORKER_TOOL_IDS})
