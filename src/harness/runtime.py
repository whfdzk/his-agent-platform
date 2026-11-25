# src/harness/runtime.py
#
# Runtime 层：工具的受控执行环境。
#
# 现状问题（改造前）：工具直接在 ToolNode 里跑，谁抛异常谁就把整条 Agent 链路带崩；
# 慢工具（药品查询 15s、OCR 90s）没有统一上限，只有各自散落的 httpx timeout；
# 重试完全靠模型"被报错后自己再调一次"，既慢又不可控。
#
# 这一层做的事情（都是可测量的）：
#   1. 策略前置：调用前过 policy.check_tool_call（Guardrail 挂在执行链上，而不是只在入口）
#   2. 超时：每次工具调用统一 wait_for，超时归一成 Observation，不让整轮挂死
#   3. 重试：只对只读工具（query_/get_/search_ 等前缀）重试，且只重试瞬时连接类错误；
#      超时与确定性错误不重试——重试只会把尾延迟翻倍；
#      写工具（建预约、开工单）绝不自动重试——重试写操作 = 重复副作用
#   4. 异常归一：任何异常都转成"模型看得懂的一句话"ToolMessage，模型据此收敛作答
#   5. 观测：计数（总数/失败/超时/重试/拒绝/by_tool）供 /admin/harness/stats 读取

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from loguru import logger

from src.core.config import get_settings
from src.harness import policy

# 这些工具带"挂起/中断"语义（转人工），超时包裹会破坏 interrupt()，一律直通
SPECIAL_TOOLS = frozenset({"request_human_handoff"})

# 只读工具前缀白名单：只有这类调用允许自动重试
READ_ONLY_PREFIXES = (
    "get_", "list_", "query_", "search_", "load_", "read_", "fetch_", "find_", "lookup_",
    "kb_", "drug_", "check_",
)

DEFAULT_TIMEOUT_SECONDS = 45.0
DEFAULT_MAX_ATTEMPTS = 2


def is_read_only(tool_name: str) -> bool:
    return any(tool_name.startswith(p) for p in READ_ONLY_PREFIXES)


@dataclass
class ToolObservation:
    """一次受控工具调用的结果。失败时带上给模型看的说明。"""

    tool: str
    ok: bool
    attempts: int
    duration_ms: int
    result: Any = field(default=None, repr=False)
    error_type: str = ""
    error: str = ""
    timed_out: bool = False
    denied: bool = False

    def as_error_text(self) -> str:
        if self.denied:
            headline = "工具调用被拒绝"
            advice = "该调用不符合平台策略，请换一种合规的方式获取信息，不要重复尝试。"
        elif self.timed_out:
            headline = "工具执行超时"
            advice = "该工具当前响应过慢，不要重复调用；请基于已有信息作答，或告知用户稍后再试。"
        else:
            headline = "工具执行失败"
            advice = "不要重复调用同一工具；请基于已有信息作答，或告知用户该项暂时无法查询。"
        detail = f"{self.error_type}: {self.error}" if self.error else "未知错误"
        return f"【{headline}】{self.tool}（耗时 {self.duration_ms}ms，尝试 {self.attempts} 次）：{detail[:200]}。{advice}"


class ToolRuntimeGateway:
    """工具执行网关：所有 worker 的工具调用都从这里过一遍。"""

    def __init__(self, *, timeout_s: float | None = None, max_attempts: int | None = None):
        settings = get_settings()
        self.timeout_s = float(
            timeout_s if timeout_s is not None
            else getattr(settings, "TOOL_CALL_TIMEOUT", DEFAULT_TIMEOUT_SECONDS)
        )
        self.max_attempts = max(
            1, int(max_attempts if max_attempts is not None
                   else getattr(settings, "TOOL_CALL_MAX_ATTEMPTS", DEFAULT_MAX_ATTEMPTS))
        )
        self._stats: dict[str, Any] = {
            "calls": 0, "ok": 0, "failed": 0, "timeouts": 0,
            "retries": 0, "denied": 0, "total_ms": 0,
            "by_tool": {},
        }

    # ── 观测 ────────────────────────────────────────────────────────────
    def stats(self) -> dict:
        calls = self._stats["calls"]
        ok = self._stats["ok"]
        return {
            "calls": calls,
            "ok": ok,
            "failed": self._stats["failed"],
            "timeouts": self._stats["timeouts"],
            "retries": self._stats["retries"],
            "denied": self._stats["denied"],
            "avg_ms": int(self._stats["total_ms"] / calls) if calls else 0,
            "success_rate": round(ok / calls, 4) if calls else 0.0,
            "by_tool": dict(self._stats["by_tool"]),
            "timeout_s": self.timeout_s,
            "max_attempts": self.max_attempts,
        }

    def reset(self) -> None:
        self._stats = {
            "calls": 0, "ok": 0, "failed": 0, "timeouts": 0,
            "retries": 0, "denied": 0, "total_ms": 0, "by_tool": {},
        }

    def _count(self, tool_name: str, key: str) -> None:
        self._stats[key] += 1
        per = self._stats["by_tool"].setdefault(tool_name, {"calls": 0, "failed": 0})
        if key == "calls":
            per["calls"] += 1
        elif key == "failed":
            per["failed"] += 1

    # ── 执行 ────────────────────────────────────────────────────────────
    async def execute(
        self,
        tool_name: str,
        args: object,
        call: Callable[[], Awaitable[Any]],
        *,
        timeout_s: float | None = None,
    ) -> ToolObservation:
        decision = policy.check_tool_call(tool_name, args)
        if not decision.allowed:
            self._count(tool_name, "calls")
            self._count(tool_name, "denied")
            logger.warning(f"[runtime] 拒绝工具调用 {tool_name}: {decision.code} {decision.reason}")
            return ToolObservation(
                tool=tool_name, ok=False, attempts=0, duration_ms=0,
                denied=True, error=decision.reason, error_type=decision.code,
            )

        timeout = float(timeout_s if timeout_s is not None else self.timeout_s)
        attempts_allowed = self.max_attempts if is_read_only(tool_name) else 1
        started = time.perf_counter()
        last_error: Exception | None = None
        timed_out = False
        attempts_made = 0

        for attempt in range(1, attempts_allowed + 1):
            attempts_made = attempt
            try:
                result = await asyncio.wait_for(call(), timeout=timeout)
                duration = int((time.perf_counter() - started) * 1000)
                self._count(tool_name, "calls")
                self._count(tool_name, "ok")
                self._stats["total_ms"] += duration
                if attempt > 1:
                    logger.info(f"[runtime] {tool_name} 第 {attempt} 次尝试成功（耗时 {duration}ms）")
                return ToolObservation(
                    tool=tool_name, ok=True, attempts=attempt, duration_ms=duration, result=result,
                )
            except asyncio.CancelledError:
                # 取消必须透传：上层在关停/超时收尾时取消协程，吞掉会变成悬挂任务
                raise
            except Exception as e:  # noqa: BLE001
                last_error = e
                timed_out = isinstance(e, (asyncio.TimeoutError, TimeoutError))
                # 只重试"瞬时连接类"错误：超时已经吃满一次预算，再重试只会把尾延迟翻倍；
                # 确定性业务错误（参数错、未收录）重试也不会变好。
                retryable = (not timed_out) and isinstance(e, (ConnectionError, OSError))
                if attempt < attempts_allowed and retryable:
                    self._count(tool_name, "retries")
                    logger.info(
                        f"[runtime] {tool_name} 第 {attempt} 次失败（{'超时' if timed_out else type(e).__name__}），重试"
                    )
                    await asyncio.sleep(min(0.5 * attempt, 2.0))
                    continue
                break

        duration = int((time.perf_counter() - started) * 1000)
        self._count(tool_name, "calls")
        self._count(tool_name, "failed")
        self._stats["total_ms"] += duration
        if timed_out:
            self._count(tool_name, "timeouts")
        error_type = type(last_error).__name__ if last_error else "UnknownError"
        error = str(last_error) if last_error else "unknown"
        logger.warning(f"[runtime] {tool_name} 执行失败（{error_type}，{duration}ms）：{error[:160]}")
        return ToolObservation(
            tool=tool_name, ok=False, attempts=attempts_made, duration_ms=duration,
            timed_out=timed_out, error_type=error_type, error=error,
        )


_gateway: ToolRuntimeGateway | None = None


def get_gateway() -> ToolRuntimeGateway:
    global _gateway
    if _gateway is None:
        _gateway = ToolRuntimeGateway()
    return _gateway


def reset_gateway() -> None:
    """测试/管理端重置单例（会把观测计数一起清零）。"""
    global _gateway
    _gateway = None
