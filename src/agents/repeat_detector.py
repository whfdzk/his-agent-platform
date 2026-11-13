"""
打转检测：检测 Agent 是否在同一个工具调用上反复循环。
借鉴 ai-commerce-interview 的实现：参数指纹 + 时间窗 + 豁免工具。

为什么需要打转检测：
- 静态轮数上限只能兜住"一直做但没做完"，兜不住"一直做同一件事"
- 模型打转时轮数往往远没到上限，预算已经烧完了
- 用参数指纹而不是工具名：同名工具换个参数就是有效进展
"""
from __future__ import annotations
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any
from loguru import logger


@dataclass
class RepeatDetector:
    """打转检测器"""

    # 时间窗：窗口内的重复算打转
    window_steps: int = 3
    # 允许的最大重复次数（超过就判定为打转）
    max_repeats: int = 2
    # 豁免工具：已知需要反复调用的工具（如轮询）
    exempt_tools: set[str] = field(default_factory=set)
    # 指纹表：记录每个指纹第一次出现的轮次
    fingerprints: dict[str, int] = field(default_factory=dict)
    # 当前轮次
    current_step: int = 0
    # 指纹表最大长度（防止内存泄漏）
    max_fingerprints: int = 256

    def __post_init__(self):
        # 默认豁免一些工具
        self.exempt_tools.update({
            "search_memory",  # 记忆检索可能多次调用
            "get_my_profile",  # 档案查询
        })

    def _make_fingerprint(self, tool_name: str, tool_args: dict[str, Any]) -> str:
        """生成工具调用指纹：工具名 + 参数排序后哈希。
        同一组参数换个键顺序就是同一次调用。
        """
        # 排序参数，保证键顺序不影响指纹
        sorted_args = json.dumps(tool_args, sort_keys=True, ensure_ascii=False)
        raw = f"{tool_name}:{sorted_args}"
        return hashlib.md5(raw.encode()).hexdigest()[:16]

    def check_call(self, tool_name: str, tool_args: dict[str, Any]) -> tuple[bool, str]:
        """检查本次工具调用是否是打转。

        Returns:
            (is_repeat, reason): 是否是打转，以及原因
        """
        # 豁免工具直接放行
        if tool_name in self.exempt_tools:
            return False, ""

        fingerprint = self._make_fingerprint(tool_name, tool_args)

        # 检查是否在时间窗内出现过
        first_seen = self.fingerprints.get(fingerprint)
        if first_seen is not None and self.current_step - first_seen < self.window_steps:
            reason = (
                f"检测到打转：工具 {tool_name} 在 {self.window_steps} 轮内被重复调用 "
                f"（第 {first_seen + 1} 轮第一次出现，第 {self.current_step + 1} 轮再次出现）"
            )
            logger.warning(f"[RepeatDetector] {reason}")
            return True, reason

        # 记录指纹
        self.fingerprints[fingerprint] = self.current_step

        # 清理过期指纹
        if len(self.fingerprints) > self.max_fingerprints:
            # 按轮次排序，清理最老的
            sorted_items = sorted(self.fingerprints.items(), key=lambda x: x[1])
            for key, _ in sorted_items[: len(self.fingerprints) - self.max_fingerprints]:
                del self.fingerprints[key]

        return False, ""

    def next_step(self) -> None:
        """进入下一轮。"""
        self.current_step += 1

    def reset(self) -> None:
        """重置检测器。"""
        self.fingerprints.clear()
        self.current_step = 0


# 全局检测器缓存（按 session_id 隔离）
_detector_cache: dict[str, RepeatDetector] = {}


def get_detector(session_id: str) -> RepeatDetector:
    """获取或创建会话级别的检测器。"""
    if session_id not in _detector_cache:
        _detector_cache[session_id] = RepeatDetector()
    return _detector_cache[session_id]


def reset_detector(session_id: str) -> None:
    """重置会话级别的检测器。"""
    if session_id in _detector_cache:
        del _detector_cache[session_id]
