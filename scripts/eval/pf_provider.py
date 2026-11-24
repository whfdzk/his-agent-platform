"""promptfoo 的 Python provider：把评测用例打到本地 /api/v1/chat/stream。

为什么走 SSE 而不是 /api/v1/chat：
- 输入拦截只能从 SSE 的 safety / error 事件看出来（非流式接口不带这个信息），
  而 gate 要算对抗阻断率和误杀率，缺了 blocked 就没法算。
- 顺带能收到 citation 事件，后面接 Ragas faithfulness 要用到 quote。

约定：
- vars.question：单轮问题
- vars.turns_json：多轮时的 JSON 字符串（promptfoo 会把 vars 里的数组压成字符串）
- vars.session_id：同一条用例固定，保证多轮落在同一会话
- 返回 output：最后一轮正文；被拦截时前置「【已拦截】」标记，让断言能表达"拦截即通过"
- 返回 metadata：blocked / error / citations / latency_ms，供 pf_to_report.py 还原门禁指标
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

DEFAULT_BASE = os.environ.get("EVAL_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
BLOCK_MARK = "【已拦截】"
BUSY_MARKERS = ("比较繁忙", "服务繁忙", "稍后再尝试", "系统繁忙", "上一条消息仍在处理")
HERE = Path(__file__).resolve().parent


def _eval_token() -> str:
    """评测账号 token：由 pf_run.py 登录后通过 EVAL_TOKEN 传入。

    为什么必须带 token：匿名请求走 persist=False 分支，handoff 时不补"已转人工"
    提示、也不发 handoff 事件，测出来的行为与真实用户不一致；且多轮会话历史不落库。
    """
    return os.environ.get("EVAL_TOKEN", "").strip()


def _append_jsonl(rel_path: str, record: dict) -> None:
    """每跑完一条立即追加落盘：中断时已完成的条目不会丢（续跑见 gen_pf_config --skip-done）。"""
    if not rel_path:
        return
    path = Path(rel_path)
    if not path.is_absolute():
        path = HERE / path
    path.parent.mkdir(parents=True, exist_ok=True)
    record.setdefault("finished_at", datetime.now().isoformat(timespec="seconds"))
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def _stream_turn(base: str, session_id: str, message: str, timeout: float) -> dict:
    body = json.dumps({"session_id": session_id, "message": message}).encode("utf-8")
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "Accept": "text/event-stream",
    }
    token = _eval_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        f"{base}/api/v1/chat/stream",
        data=body,
        headers=headers,
        method="POST",
    )
    text: list[str] = []
    cites: list[dict] = []
    counts: dict[str, int] = {}
    blocked = False
    error = ""
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if resp.status != 200:
            return {"text": "", "blocked": False, "error": f"HTTP {resp.status}", "citations": [], "latency_ms": 0}
        for raw in resp:
            line = raw.decode("utf-8", "ignore").strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload:
                continue
            try:
                ev = json.loads(payload)
            except json.JSONDecodeError:
                continue
            etype = ev.get("type")
            counts[etype] = counts.get(etype, 0) + 1
            if etype == "token":
                text.append(ev.get("content") or "")
            elif etype == "citation":
                for it in ev.get("items") or []:
                    cites.append(
                        {
                            "doc_name": it.get("doc_name"),
                            "doc_type": it.get("doc_type"),
                            "score": it.get("score"),
                            "quote": (it.get("quote") or "")[:400],
                        }
                    )
            elif etype == "safety":
                # 输入拦截的 safety 事件不带 content；带 content 的是输出改写，不算拦截
                if not ev.get("content"):
                    blocked = True
                    break
            elif etype == "error":
                code = str(ev.get("code") or "")
                msg = str(ev.get("message") or "")
                if code in ("safety", "blocked") or "安全" in msg:
                    blocked = True
                else:
                    error = f"{code}: {msg}"
                break
            elif etype == "done":
                break
    return {
        "text": "".join(text),
        "blocked": blocked,
        "error": error,
        "citations": cites,
        "latency_ms": int((time.monotonic() - t0) * 1000),
        "events": counts,
    }


def _stream_with_retry(base: str, session_id: str, message: str, timeout: float, retries: int = 2) -> dict:
    last = None
    for attempt in range(retries + 1):
        try:
            got = _stream_turn(base, session_id, message, timeout)
        except urllib.error.HTTPError as e:
            got = {"text": "", "blocked": False, "error": f"HTTP {e.code}", "citations": [], "latency_ms": 0}
            if e.code == 429 or e.code >= 500:
                if attempt < retries:
                    time.sleep(5)
                    continue
            return got
        except Exception as e:  # 超时 / 连接错误
            got = {"text": "", "blocked": False, "error": f"{type(e).__name__}: {e}", "citations": [], "latency_ms": 0}
            if attempt < retries:
                time.sleep(5)
                continue
            return got
        last = got
        break
    return last or {"text": "", "blocked": False, "error": "no result", "citations": [], "latency_ms": 0}


def _resolve_turns(vars_: dict) -> list[str]:
    raw = vars_.get("turns_json")
    if raw:
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = [raw]
        else:
            parsed = raw
        if isinstance(parsed, list) and parsed:
            return [str(t) for t in parsed]
    return [str(vars_.get("question", ""))]


def call_api(prompt, options, context):  # noqa: ARG001 - promptfoo 固定签名
    vars_ = (context or {}).get("vars") or {}
    cfg = (options or {}).get("config") or {}
    base = (cfg.get("base_url") or DEFAULT_BASE).rstrip("/")
    timeout = float(cfg.get("timeout_ms") or 90.0)

    turns = _resolve_turns(vars_)
    session_id = vars_.get("session_id") or "pf-anonymous"

    result = {"text": "", "blocked": False, "error": "", "citations": [], "latency_ms": 0}
    latencies: list[int] = []
    for text in turns:
        # 同一会话的上一轮流式还没收尾时，后端会回 session_busy。
        # 这不是内容失败也不是环境故障，等锁释放后重发即可（否则会记成 error 污染门禁）。
        for attempt in range(3):
            result = _stream_with_retry(base, session_id, text, timeout)
            busy_now = "session_busy" in (result["error"] or "") or any(
                m in (result["text"] or "") for m in ("session_busy", "上一条消息仍在处理")
            )
            if not busy_now:
                break
            time.sleep(3 + attempt * 2)
        if result["error"] and "session_busy" in result["error"]:
            # 重试后仍忙：留空正文而不是记 error，避免把锁竞争算成请求失败
            result = {**result, "error": "", "text": ""}
        # 偶发空正文（无拦截、无错误）：多半是 SSE 读流中途断掉，换一个会话重试一次，
        # 否则会把环境抖动记成内容失败，污染门禁。
        if not result["text"] and not result["blocked"] and not result["error"]:
            retry = _stream_with_retry(base, f"{session_id}-r1", text, timeout)
            latencies.append(retry["latency_ms"])
            if retry["text"] or retry["blocked"] or retry["error"]:
                result = retry
        latencies.append(result["latency_ms"])
        if result["blocked"]:
            break  # 拦截后同一会话不再发下一轮

    text = result["text"]
    if result["blocked"]:
        text = BLOCK_MARK + text
    busy = (not result["blocked"]) and any(m in text for m in BUSY_MARKERS)
    if result["error"]:
        text = f"__ERROR__{result['error']}"

    _append_jsonl(
        str(cfg.get("jsonl") or ""),
        {
            "id": vars_.get("case_id") or vars_.get("session_id"),
            "question": vars_.get("question"),
            "turns": turns,
            "session_id": session_id,
            "output": text,
            "blocked": result["blocked"],
            "busy": busy,
            "error": result["error"],
            "citations": result["citations"],
            "latency_ms": latencies,
            "events": result.get("events") or {},
        },
    )

    return {
        "output": text,
        "metadata": {
            "blocked": result["blocked"],
            "busy": busy,
            "error": result["error"],
            "citations": result["citations"],
            "events": result.get("events") or {},
            "latency_ms": latencies[-1] if latencies else 0,
            "latencies_ms": latencies,
            "turns": len(turns),
        },
    }
