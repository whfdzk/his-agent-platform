"""
幻觉检查加强版：校验回答是否基于检索结果，不一致时自动降级。
借鉴 ai-commerce-interview 的实现。

为什么需要加强幻觉检查：
- 医疗场景对准确性要求极高，幻觉可能导致严重后果
- 现有的检查失败时默认返回 True（可信），这是不安全的
- 需要有降级机制：检测到幻觉时，不要直接回答，而是提示用户咨询医生
"""
from __future__ import annotations
import json
from dataclasses import dataclass, field
from typing import Any
from loguru import logger
from langchain_core.messages import SystemMessage
from langchain_core.language_models import BaseChatModel

from src.knowledge.llm_call import UTIL_TIMEOUT, call_llm
from src.knowledge.prompts import HALLUCINATION_CHECK_PROMPT


@dataclass
class HallucinationResult:
    """幻觉检查结果。"""
    is_grounded: bool  # 是否基于检索结果
    confidence: float  # 置信度（0-1）
    unsupported_claims: list[str] = field(default_factory=list)  # 不被支持的声明
    needs_handoff: bool = False  # 是否需要交接给人工
    warning_message: str = ""  # 给用户的警告信息

    def to_dict(self) -> dict[str, Any]:
        return {
            "is_grounded": self.is_grounded,
            "confidence": self.confidence,
            "unsupported_claims": self.unsupported_claims,
            "needs_handoff": self.needs_handoff,
            "warning_message": self.warning_message,
        }


# 默认阈值
DEFAULT_THRESHOLD = 0.7  # 低于这个置信度就认为是幻觉
HIGH_RISK_THRESHOLD = 0.5  # 低于这个置信度就需要交接


async def check_hallucination_enhanced(
    question: str,
    evidence: str,
    answer: str,
    llm: BaseChatModel,
    threshold: float = DEFAULT_THRESHOLD,
    high_risk_threshold: float = HIGH_RISK_THRESHOLD,
) -> HallucinationResult:
    """
    加强版幻觉检测：校验回答是否基于检索结果。

    Args:
        question: 用户问题
        evidence: 检索到的证据
        answer: 模型生成的回答
        llm: 大模型
        threshold: 正常置信度阈值
        high_risk_threshold: 高风险阈值（低于这个就需要交接）

    Returns:
        HallucinationResult: 检查结果
    """
    prompt = HALLUCINATION_CHECK_PROMPT.format(
        question=question, evidence=evidence, answer=answer,
    )

    try:
        response = await call_llm(
            llm, [SystemMessage(content=prompt)],
            timeout=UTIL_TIMEOUT, label="引用核查",
        )
        if response is None:
            # 交给下面的 except 走 fail-closed（不可信 + 建议咨询医生）
            raise RuntimeError(f"引用核查超时（{UTIL_TIMEOUT}s）")
        content = (response.content or "").strip()
        if "```" in content:
            content = content.split("```")[1].lstrip("json").strip()
        result = json.loads(content)

        confidence = result.get("confidence", 0.0)
        unsupported_claims = result.get("unsupported_claims", [])
        is_grounded = result.get("is_grounded", False) and confidence >= threshold

        # 判断是否需要交接
        needs_handoff = confidence < high_risk_threshold or len(unsupported_claims) > 2

        # 生成警告信息
        warning_message = ""
        if not is_grounded and unsupported_claims:
            warning_message = (
                "注意：以下内容未在检索资料中找到充分依据，仅供参考：\n"
                + "\n".join(f"- {claim}" for claim in unsupported_claims[:3])
                + "\n\n建议：以上信息请结合专业医生意见，不要自行用药或调整治疗方案。"
            )
        elif needs_handoff:
            warning_message = (
                "由于问题涉及复杂医疗决策，为确保安全，建议您咨询专业医生。"
            )

        result = HallucinationResult(
            is_grounded=is_grounded,
            confidence=confidence,
            unsupported_claims=unsupported_claims,
            needs_handoff=needs_handoff,
            warning_message=warning_message,
        )

        logger.info(
            f"[HallucinationCheck] grounded={is_grounded}, confidence={confidence:.2f}, "
            f"unsupported={len(unsupported_claims)}, handoff={needs_handoff}"
        )
        return result

    except Exception as e:
        # 失败时默认返回不可信（fail-closed），而不是默认可信
        logger.warning(f"幻觉检测失败，默认标记为不可信: {e}")
        return HallucinationResult(
            is_grounded=False,
            confidence=0.0,
            unsupported_claims=[],
            needs_handoff=True,
            warning_message="由于系统原因无法验证回答的准确性，建议您咨询专业医生。",
        )


# 保留旧函数名，向后兼容
async def check_hallucination(
    question: str,
    evidence: str,
    answer: str,
    llm: BaseChatModel,
    threshold: float = 0.7,
) -> dict:
    """旧版幻觉检测接口（向后兼容）。"""
    result = await check_hallucination_enhanced(
        question=question,
        evidence=evidence,
        answer=answer,
        llm=llm,
        threshold=threshold,
    )
    return result.to_dict()
