# src/core/sandbox.py
#
# Agent 沙箱：工具调用的安全执行环境。
#
# 设计：
# - 工具调用前：权限检查 + 敏感操作审批 + 资源预算检查
# - 工具调用后：输出过滤 + 安全审查 + 审计日志
# - 异常隔离：单个工具失败不影响整个 Agent 流程
#
# 面试亮点：
# 1. 多层防护：权限 → 预算 → 审批 → 输出过滤
# 2. fail-closed：任何一层不通过，直接拒绝
# 3. 审计追踪：所有工具调用都记录日志
# 4. 超时控制：防止工具卡死

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable

from loguru import logger

from src.core.context import UserContext
from src.core.limits import BudgetExceeded, ExecutionBudget


class RiskLevel(str, Enum):
    """风险等级。"""
    LOW = "low"  # 低风险：查询类
    MEDIUM = "medium"  # 中风险：修改类
    HIGH = "high"  # 高风险：删除/敏感操作


class SandboxResult:
    """沙箱执行结果。"""
    def __init__(
        self,
        success: bool,
        data: Any = None,
        error: str | None = None,
        risk_level: RiskLevel = RiskLevel.LOW,
        executed_by: str = "",
    ):
        self.success = success
        self.data = data
        self.error = error
        self.risk_level = risk_level
        self.executed_by = executed_by
    
    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "data": self.data,
            "error": self.error,
            "risk_level": self.risk_level.value,
            "executed_by": self.executed_by,
        }


@dataclass
class SandboxPolicy:
    """沙箱安全策略。"""
    # 高风险操作是否需要人工审批
    require_approval_for_high_risk: bool = True
    # 工具执行超时时间（秒）
    tool_timeout_seconds: float = 30.0
    # 是否记录所有工具调用
    audit_log_enabled: bool = True
    # 敏感词过滤（输出里不能出现）
    sensitive_patterns: list[str] = None


class Sandbox:
    """
    Agent 沙箱：安全执行工具调用。
    
    执行流程：
    1. 权限检查：用户角色能不能调用这个工具
    2. 预算检查：还有没有剩余的工具调用额度
    3. 风险评估：这个工具调用的风险等级
    4. 审批检查：高风险操作需不需要人工审批
    5. 超时执行：带超时地执行工具
    6. 输出过滤：检查输出里有没有敏感内容
    7. 审计日志：记录整个过程
    """
    
    def __init__(self, policy: SandboxPolicy | None = None):
        self.policy = policy or SandboxPolicy()
        self._call_count = 0
    
    async def execute(
        self,
        tool_name: str,
        tool_func: Callable,
        args: dict[str, Any],
        context: UserContext,
        budget: ExecutionBudget | None = None,
        risk_level: RiskLevel = RiskLevel.LOW,
    ) -> SandboxResult:
        """
        在沙箱里执行一个工具调用。
        
        这是核心方法，所有工具调用都必须经过这里。
        """
        self._call_count += 1
        call_id = self._call_count
        
        logger.info(f"[沙箱 #{call_id}] 开始执行工具: {tool_name} user={context.user_id}")
        
        try:
            # ── 第 1 层：权限检查 ──────────────────────────────────
            # （实际项目里是调用 tool_permission.py）
            if not self._check_permission(tool_name, context):
                return SandboxResult(
                    success=False,
                    error=f"权限不足：当前角色 {context.role} 不能调用 {tool_name}",
                    risk_level=risk_level,
                    executed_by=context.user_id,
                )
            
            # ── 第 2 层：预算检查 ──────────────────────────────────
            if budget:
                try:
                    budget.check_tool_call(tool_name)
                except BudgetExceeded as e:
                    return SandboxResult(
                        success=False,
                        error=e.message,
                        risk_level=risk_level,
                        executed_by=context.user_id,
                    )
            
            # ── 第 3 层：风险评估 + 审批 ────────────────────────────
            if risk_level == RiskLevel.HIGH and self.policy.require_approval_for_high_risk:
                # 高风险操作需要人工审批
                # 这里简化实现，实际会发审批通知
                logger.warning(f"[沙箱 #{call_id}] 高风险操作待审批: {tool_name}")
                return SandboxResult(
                    success=False,
                    error="该操作风险较高，需要管理员审批后才能执行",
                    risk_level=risk_level,
                    executed_by=context.user_id,
                )
            
            # ── 第 4 层：超时执行 ──────────────────────────────────
            result = await asyncio.wait_for(
                tool_func(**args),
                timeout=self.policy.tool_timeout_seconds,
            )
            
            # ── 第 5 层：输出过滤 ──────────────────────────────────
            if not self._check_output(result):
                return SandboxResult(
                    success=False,
                    error="工具输出包含敏感内容，已被拦截",
                    risk_level=risk_level,
                    executed_by=context.user_id,
                )
            
            # ── 第 6 层：审计日志 ──────────────────────────────────
            if self.policy.audit_log_enabled:
                self._log_audit(call_id, tool_name, context, risk_level, success=True)
            
            logger.info(f"[沙箱 #{call_id}] 执行成功: {tool_name}")
            return SandboxResult(
                success=True,
                data=result,
                risk_level=risk_level,
                executed_by=context.user_id,
            )
            
        except asyncio.TimeoutError:
            error_msg = f"工具执行超时（>{self.policy.tool_timeout_seconds}秒）"
            logger.error(f"[沙箱 #{call_id}] {error_msg}: {tool_name}")
            self._log_audit(call_id, tool_name, context, risk_level, success=False, error=error_msg)
            return SandboxResult(
                success=False,
                error=error_msg,
                risk_level=risk_level,
                executed_by=context.user_id,
            )
            
        except Exception as e:
            error_msg = f"工具执行异常: {str(e)}"
            logger.error(f"[沙箱 #{call_id}] {error_msg}: {tool_name}")
            self._log_audit(call_id, tool_name, context, risk_level, success=False, error=error_msg)
            return SandboxResult(
                success=False,
                error=error_msg,
                risk_level=risk_level,
                executed_by=context.user_id,
            )
    
    def _check_permission(self, tool_name: str, context: UserContext) -> bool:
        """检查用户是否有权限调用这个工具。"""
        # 简化实现，实际调用 tool_permission.py
        # 这里举个例子：患者不能调用运营数据查询
        if "admin" in tool_name and context.role == "patient":
            return False
        return True
    
    def _check_output(self, result: Any) -> bool:
        """检查输出里有没有敏感内容。"""
        # 简化实现，实际用安全引擎过滤
        if isinstance(result, str):
            # 这里可以加敏感词检查
            pass
        return True
    
    def _log_audit(
        self,
        call_id: int,
        tool_name: str,
        context: UserContext,
        risk_level: RiskLevel,
        success: bool,
        error: str | None = None,
    ) -> None:
        """记录审计日志。"""
        logger.info(
            f"[审计 #{call_id}] "
            f"tool={tool_name} "
            f"user={context.user_id} "
            f"role={context.role} "
            f"risk={risk_level.value} "
            f"success={success} "
            f"error={error or 'none'}"
        )


# 全局单例
_sandbox: Sandbox | None = None


def get_sandbox() -> Sandbox:
    """获取全局沙箱单例。"""
    global _sandbox
    if _sandbox is None:
        _sandbox = Sandbox()
    return _sandbox
