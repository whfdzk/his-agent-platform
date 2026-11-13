"""
Handoff 交接：当 Worker Agent 发现问题超出自己的能力范围时，
主动交接给 Supervisor 或其他 Agent。

为什么需要 Handoff：
- Agent 的失败通常已经做了一半（查了药品、问了知识库）
- 如果只是返回错误，接手的人必须从零重问一遍，前面几轮的 token 全白花
- 交接包必须能回答四个问题：用户要什么、做到哪一步、为什么停、接手后怎么做
"""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from loguru import logger


class HandoffReason(str, Enum):
    """需要交接的典型原因。"""

    OUT_OF_SCOPE = "out_of_scope"
    """超出能力范围：问题不在当前 Worker 的职责内。"""

    NEED_SUPERVISOR = "need_supervisor"
    """需要 Supervisor 重新调度：问题需要多个 Agent 协作。"""

    HIGH_RISK = "high_risk_confirmation"
    """高风险动作待确认：比如用药建议、剂量调整，需要人工确认。"""

    LOOP_DETECTED = "loop_detected"
    """检测到打转：模型在重复同一调用。"""

    BUDGET_EXCEEDED = "budget_exceeded"
    """预算耗尽：token/时间用完了。"""


@dataclass
class HandoffPacket:
    """交接包：给下一位执行者的输入。"""

    reason: str
    summary: str  # 用户到底要什么
    from_agent: str  # 从哪个 Agent 交接出来
    completed_steps: int = 0  # 已经做到哪一步
    tool_results: list[dict[str, Any]] = field(default_factory=list)  # 已经拿到的工具结果
    failure: str | None = None  # 为什么停
    next_actions: list[str] = field(default_factory=list)  # 接手后建议怎么做
    trace_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        """转换为字典，方便 JSON 序列化和传递。"""
        return {
            "reason": self.reason,
            "summary": self.summary,
            "from_agent": self.from_agent,
            "completed_steps": self.completed_steps,
            "tool_results": self.tool_results,
            "failure": self.failure,
            "next_actions": self.next_actions,
            "trace_id": self.trace_id,
        }


def create_handoff(
    reason: HandoffReason,
    summary: str,
    from_agent: str,
    failure: str | None = None,
    next_actions: list[str] | None = None,
    completed_steps: int = 0,
    tool_results: list[dict[str, Any]] | None = None,
) -> HandoffPacket:
    """创建交接包的便捷函数。"""
    # 根据原因生成默认的下一步建议
    if next_actions is None:
        next_actions = _default_next_actions(reason)

    packet = HandoffPacket(
        reason=reason.value,
        summary=summary,
        from_agent=from_agent,
        completed_steps=completed_steps,
        tool_results=tool_results or [],
        failure=failure,
        next_actions=next_actions,
    )

    logger.info(f"[Handoff] 从 {from_agent} 交接，原因={reason.value}")
    return packet


def _default_next_actions(reason: HandoffReason) -> list[str]:
    """根据交接原因生成默认的下一步建议。"""
    suggestions = {
        HandoffReason.OUT_OF_SCOPE: [
            "重新评估用户意图，调度给更合适的专项 Agent",
            "如果是多领域问题，拆分成多个子问题分别处理",
        ],
        HandoffReason.NEED_SUPERVISOR: [
            "由 Supervisor 重新调度，协调多个 Worker 协作",
        ],
        HandoffReason.HIGH_RISK: [
            "向用户确认高风险动作是否继续",
            "建议用户咨询专业医生，不要自行决策",
        ],
        HandoffReason.LOOP_DETECTED: [
            "中断当前 Agent，由 Supervisor 重新规划执行路径",
            "简化问题，换一种方式回答",
        ],
        HandoffReason.BUDGET_EXCEEDED: [
            "总结当前已获取的信息，直接回答用户",
            "建议用户拆分问题，分多次提问",
        ],
    }
    return suggestions.get(reason, ["重新评估问题，选择合适的处理方式"])


class HandoffMixin:
    """Worker Agent 可以继承这个 Mixin，获得 Handoff 能力。"""

    agent_name: str = "unknown"

    def handoff(
        self,
        reason: HandoffReason,
        summary: str,
        failure: str | None = None,
        next_actions: list[str] | None = None,
    ) -> HandoffPacket:
        """创建交接包。"""
        return create_handoff(
            reason=reason,
            summary=summary,
            from_agent=self.agent_name,
            failure=failure,
            next_actions=next_actions,
        )
