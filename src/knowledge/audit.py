# src/knowledge/audit.py
#
# 知识检索审计：日志 + 落库双写。
#
# 此前只写 loguru：容器重建/日志轮转后，"谁在什么时候查了什么、答了什么、
# 有没有文档支撑"全部不可回溯。医疗场景要求可查证，因此日志照旧保留（便于
# 实时排障），同时把脱敏截断后的链路写入 knowledge_audit_logs。
#
# 落库走 persist_queue 批量写入：审计不能成为主链路的同步阻塞点。

from __future__ import annotations
import time

from loguru import logger

from src.core.persist_queue import PersistItem, enqueue
from src.core.redaction import redact_text
from src.core.langfuse_tracing import current_trace


class QueryAuditLog:
    """查询审计日志记录器。记录每次知识检索的完整链路。"""

    @staticmethod
    def log(
        user_id: str,
        role: str,
        question: str,
        intent: str,
        channels: list[str],
        answer_preview: str,
        duration_ms: float,
        hallucination_check: dict | None = None,
        hit_count: int = 0,
    ) -> None:
        grounded = None
        if hallucination_check:
            grounded = hallucination_check.get("is_grounded")

        logger.bind(audit=True).info(
            "knowledge_query | "
            f"user={user_id} | role={role} | intent={intent} | "
            f"channels={channels} | duration={duration_ms:.0f}ms | "
            f"grounded={grounded if grounded is not None else 'N/A'} | "
            f"question={question[:80]} | "
            f"answer={answer_preview[:80]}"
        )

        trace = current_trace()
        # 命中数与核查结论优先取调用方传值；没传就从当前 trace 兜底。
        # 检索通道（doc_rag/graph_rag）命中时只写 trace、不改函数签名，
        # 因此这里读 trace 才是"全通道统一"的取值方式。
        if hit_count <= 0 and trace is not None:
            hit_count = trace.retrieval_count
        if grounded is None and trace is not None:
            grounded = getattr(trace, "grounded", None)
        payload = {
            "trace_id": trace.trace_id if trace else "",
            "request_id": trace.request_id if trace else "",
            "user_id": user_id or "",
            "role": role or "",
            "intent": intent or "",
            "channels": list(channels or []),
            # 截断 + PHI 正则：审计要能定位问题，但不该成为病历原文的第二份副本
            "question": redact_text(question, 200),
            "answer_preview": redact_text(answer_preview, 200),
            "duration_ms": float(duration_ms or 0.0),
            "grounded": grounded,
            "hit_count": int(hit_count or 0),
        }
        try:
            enqueue(PersistItem(kind="knowledge_audit", payload=payload))
        except Exception as e:
            # 审计落库失败不能反噬检索主链路
            logger.warning(f"knowledge_audit 入队失败（不影响主流程）: {e}")



class Timer:
    """简单计时器，用于测量检索耗时。"""

    def __init__(self):
        self._start = None

    def __enter__(self):
        self._start = time.perf_counter()
        return self

    def __exit__(self, *args):
        pass

    @property
    def elapsed_ms(self) -> float:
        if self._start is None:
            return 0.0
        return (time.perf_counter() - self._start) * 1000
