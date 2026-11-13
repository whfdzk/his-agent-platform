"""
证据抽取模块：从工具返回结果中提取结构化证据。
用于回答引用，提升医疗可信度。
"""
from __future__ import annotations
import re
from typing import Any
from loguru import logger


class Evidence:
    """单条证据"""
    def __init__(self, source: str, content: str, evidence_type: str = "general"):
        self.source = source  # 来源：drug_db / knowledge_base / report_ocr
        self.content = content  # 证据内容
        self.evidence_type = evidence_type  # 证据类型：dosage / indication / contraindication / general

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "content": self.content,
            "type": self.evidence_type,
        }


def extract_drug_evidence(text: str) -> list[Evidence]:
    """从药品查询结果中提取结构化证据。"""
    evidences = []

    # 提取剂量信息
    dosage_patterns = [
        r"剂量[：:]\s*([^\n]+)",
        r"用法用量[：:]\s*([^\n]+)",
        r"每次\s*(\d+\s*(?:mg|g|ml|片|粒|袋))",
        r"每日\s*(\d+\s*次)",
    ]
    for pattern in dosage_patterns:
        matches = re.findall(pattern, text)
        for m in matches:
            evidences.append(Evidence(
                source="drug_db",
                content=m.strip(),
                evidence_type="dosage",
            ))

    # 提取适应症
    indication_patterns = [
        r"适应症[：:]\s*([^\n]+)",
        r"用于治疗[：:]\s*([^\n]+)",
    ]
    for pattern in indication_patterns:
        matches = re.findall(pattern, text)
        for m in matches:
            evidences.append(Evidence(
                source="drug_db",
                content=m.strip(),
                evidence_type="indication",
            ))

    # 提取禁忌症
    contraindication_patterns = [
        r"禁忌[：:]\s*([^\n]+)",
        r"禁用[：:]\s*([^\n]+)",
    ]
    for pattern in contraindication_patterns:
        matches = re.findall(pattern, text)
        for m in matches:
            evidences.append(Evidence(
                source="drug_db",
                content=m.strip(),
                evidence_type="contraindication",
            ))

    # 如果没有提取到结构化证据，返回一条通用证据
    if not evidences and text:
        evidences.append(Evidence(
            source="drug_db",
            content=text[:200],
            evidence_type="general",
        ))

    return evidences


def extract_knowledge_evidence(text: str, source: str = "knowledge_base") -> list[Evidence]:
    """从知识库检索结果中提取证据。"""
    evidences = []

    # 简单按句子分割，取前3条作为证据
    sentences = re.split(r"[。！？\n]", text)
    sentences = [s.strip() for s in sentences if len(s.strip()) > 10]

    for s in sentences[:3]:
        evidences.append(Evidence(
            source=source,
            content=s,
            evidence_type="general",
        ))

    return evidences


def extract_evidence(tool_name: str, tool_result: str) -> list[dict]:
    """
    统一入口：根据工具名从结果中提取证据。
    返回 dict 列表，方便直接 JSON 序列化。
    """
    try:
        if "drug" in tool_name.lower():
            evidences = extract_drug_evidence(tool_result)
        elif "knowledge" in tool_name.lower() or "rag" in tool_name.lower():
            evidences = extract_knowledge_evidence(tool_result)
        else:
            # 默认：取前200字作为通用证据
            evidences = [Evidence(
                source=tool_name,
                content=tool_result[:200],
                evidence_type="general",
            )]

        return [e.to_dict() for e in evidences]

    except Exception as e:
        logger.warning(f"证据抽取失败: {e}")
        return []
