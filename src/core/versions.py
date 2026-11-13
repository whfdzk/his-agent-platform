# src/core/versions.py
#
# Agent / Prompt / 应用版本登记表。
#
# 全链路追踪要求每次执行都能回答"用的是哪个版本的 Agent 与 Prompt"。
# 这些版本号跟随代码演进，集中在一处维护，避免日志/数据库里出现空白。

from __future__ import annotations

APP_VERSION = "1.1.0"

# agent 名 -> 元信息。consultation 对应问诊子图（inquiry graph）。
AGENT_REGISTRY: dict[str, dict[str, str]] = {
    "supervisor": {
        "version": "1.4.0",
        "prompt_version": "supervisor-v4",
        "description": "总调度 Agent：路由到问诊/药物/知识/报告/运营",
    },
    "consultation": {
        "version": "1.3.0",
        "prompt_version": "inquiry-graph-v3",
        "description": "问诊 Agent：症状抽取、澄清、鉴别、挂号移交",
    },
    "inquiry": {
        "version": "1.3.0",
        "prompt_version": "inquiry-graph-v3",
        "description": "问诊 Agent（兼容别名）",
    },
    "drug": {
        "version": "1.1.0",
        "prompt_version": "drug-v2",
        "description": "药物 Agent：相互作用与处方审查",
    },
    "knowledge": {
        "version": "1.1.0",
        "prompt_version": "knowledge-v2",
        "description": "知识 Agent：Milvus + Neo4j 混合检索",
    },
    "report": {
        "version": "1.2.0",
        "prompt_version": "report-v2",
        "description": "报告解读 Agent",
    },
    "operation": {
        "version": "1.0.0",
        "prompt_version": "operation-v1",
        "description": "运营数据 Agent：NL2SQL",
    },
    "safety": {
        "version": "1.0.0",
        "prompt_version": "safety-rules-v1",
        "description": "Safety Engine：危险信号与输出审核",
    },
}

_DEFAULT_META = {"version": "0.0.0", "prompt_version": "unknown", "description": ""}


def agent_meta(name: str) -> dict[str, str]:
    """取 Agent 元信息；未登记的 Agent 返回占位值，保证追踪字段不为 None。"""
    return AGENT_REGISTRY.get(name, _DEFAULT_META)
