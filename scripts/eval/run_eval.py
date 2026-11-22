"""离线评测：用真实 SSE 跑一遍评测集，判定命中情况并输出报告。

为什么需要它：P0/P1 期间修过"诊断结论荒谬""低置信度冒充诊断""回答重复两遍""VL 模型 404"
等问题，但每次都是靠临时脚本单点验证。改 prompt、换模型、调检索参数之后，没有任何东西
能告诉你"上次修好的东西这次还成立吗"。这个脚本就是那张回归网。

判定口径（避免把模型表述钉死成标准答案）：
- expect_any：回答里命中任意一个关键词即通过（基于知识库原文 / 真实库数据的核心实体）
- must_not_include：出现任意一个即失败（确诊式表述、劝阻就医等）
- 被安全层拦截（blocked）视为通过，但单独计数 —— 拦截是预期的正确行为
- 带 turns 的用例在同一个 session 里连发多轮，判定基于全部轮次拼接的回答：
  问诊类问题是"追问→结论"的多轮流程，只发第一轮永远拿不到科室名
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:8000"
ROOT = Path(__file__).resolve().parents[2]
CASES_PATH = ROOT / "scripts" / "eval" / "cases.json"
REPORT_DIR = ROOT / "doc" / "eval-reports"

PHONE = "13800138001"
PASSWORD = "test123456"


def norm(text: str) -> str:
    """去空白后比对：模型常写成 '5 克' / '140 mmHg'，空白差异不该影响判定。"""
    return re.sub(r"\s+", "", text or "")


async def login(client: httpx.AsyncClient) -> str:
    r = await client.post(f"{BASE}/api/v1/auth/login", json={"phone": PHONE, "password": PASSWORD})
    if r.status_code == 429:
        raise RuntimeError("登录被限流（5 次/300s）：请等限流窗口或先清 rl:login:* 再跑")
    r.raise_for_status()
    return r.json()["token"]


async def one_turn(
    client: httpx.AsyncClient, token: str, session_id: str, message: str, turn_idx: int
) -> tuple[str, bool, str]:
    """发一轮消息，读完 SSE，返回 (回答文本, 是否被安全拦截, 错误信息)。"""
    text, blocked, error = "", False, ""
    body = {
        "session_id": session_id,
        "message": message,
        "client_message_id": f"cm-{uuid.uuid4().hex[:12]}-{turn_idx}",
    }
    try:
        async with client.stream(
            "POST", f"{BASE}/api/v1/chat/stream", json=body,
            headers={"Authorization": f"Bearer {token}", "Accept": "text/event-stream"},
            timeout=httpx.Timeout(180.0, connect=10.0),
        ) as resp:
            if resp.status_code != 200:
                error = f"HTTP {resp.status_code}"
                await resp.aread()
            else:
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if not raw:
                        continue
                    try:
                        ev = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    etype = ev.get("type")
                    if etype == "token":
                        text += ev.get("content") or ""
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
    except (httpx.HTTPError, asyncio.TimeoutError) as e:
        error = f"{type(e).__name__}"
    return text, blocked, error


async def run_case(client: httpx.AsyncClient, token: str, case: dict, run_tag: str) -> dict:
    session_id = f"{run_tag}-{case['id']}"
    turns = case.get("turns") or [case["question"]]

    t0 = time.monotonic()
    text, blocked, error = "", False, ""
    for i, msg in enumerate(turns):
        part, blk, err = await one_turn(client, token, session_id, msg, i)
        if part:
            text += part + "\n"
        blocked = blocked or blk
        if err:
            error = f"第{i + 1}轮 {err}"
            break
        if blk:
            break

    latency_ms = int((time.monotonic() - t0) * 1000)
    passed, reason = judge(case, text, blocked, error)
    return {
        "id": case["id"],
        "category": case["category"],
        "question": " → ".join(turns),
        "passed": passed,
        "reason": reason,
        "latency_ms": latency_ms,
        "chars": len(text),
        "blocked": blocked,
        "error": error,
        "answer": text[:400],
    }


def judge(case: dict, text: str, blocked: bool, error: str) -> tuple[bool, str]:
    if error:
        return False, f"请求失败: {error}"
    if blocked:
        return True, "安全层拦截（符合预期）"

    body = norm(text)
    if not body:
        return False, "回答为空"

    banned = case.get("must_not_include") or []
    for word in banned:
        if norm(word) in body:
            return False, f"命中禁止表述「{word}」"

    expects = case.get("expect_any") or []
    if expects:
        hit = [w for w in expects if norm(w) in body]
        if not hit:
            return False, f"未命中任一期望词 {expects}"
        return True, f"命中「{hit[0]}」"
    return True, "无硬性期望"


def sort_by_category(results: list[dict]) -> dict:
    out: dict[str, dict] = {}
    for r in results:
        c = out.setdefault(r["category"], {"total": 0, "passed": 0})
        c["total"] += 1
        c["passed"] += 1 if r["passed"] else 0
    return out


def pct(values: list[int], p: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(round((p / 100) * (len(ordered) - 1))))
    return ordered[idx]


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 条（调试用）")
    ap.add_argument("--only", default="", help="只跑指定类别，逗号分隔，如 kb,safety")
    ap.add_argument("--concurrency", type=int, default=2,
                    help="并发答题数，默认 2（后端单用户并发上限就是 2，超过会直接 busy）")
    ap.add_argument("--token", default="", help="复用已有 token，避免反复登录触发限流")
    ap.add_argument("--no-baseline", action="store_true", help="不读也不写基线")
    args = ap.parse_args()

    data = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    cases = data["cases"]
    if args.only:
        cats = {c.strip() for c in args.only.split(",") if c.strip()}
        cases = [c for c in cases if c["category"] in cats]
    if args.limit:
        cases = cases[: args.limit]

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    baseline_path = REPORT_DIR / "baseline.json"
    baseline = {}
    if not args.no_baseline and baseline_path.exists():
        try:
            baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            baseline = {}

    run_tag = f"eval-{datetime.now().strftime('%m%d%H%M')}"
    sem = asyncio.Semaphore(max(1, args.concurrency))

    async with httpx.AsyncClient() as client:
        token = args.token or await login(client)

        async def one(case: dict) -> dict:
            async with sem:
                r = await run_case(client, token, case, run_tag)
                mark = "PASS" if r["passed"] else "FAIL"
                print(f"[{mark}] {r['id']:<12} {r['latency_ms']:>6}ms {r['chars']:>4}字  {r['reason']}")
                return r

        t0 = time.monotonic()
        results = await asyncio.gather(*(one(c) for c in cases))
        duration = time.monotonic() - t0

    passed = sum(1 for r in results if r["passed"])
    total = len(results)
    by_cat = sort_by_category(results)
    latencies = [r["latency_ms"] for r in results]

    report = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "duration_s": round(duration, 1),
        "total": total,
        "passed": passed,
        "pass_rate": round(passed / total, 3) if total else 0,
        "by_category": by_cat,
        "latency": {"p50": pct(latencies, 50), "p95": pct(latencies, 95), "max": max(latencies) if latencies else 0},
        "blocked_count": sum(1 for r in results if r["blocked"]),
        "avg_chars": round(sum(r["chars"] for r in results) / total, 1) if total else 0,
        "results": results,
    }

    regressions, fixes = [], []
    if baseline.get("results"):
        old = {r["id"]: r["passed"] for r in baseline["results"]}
        for r in results:
            if r["id"] in old:
                if old[r["id"]] and not r["passed"]:
                    regressions.append({"id": r["id"], "reason": r["reason"]})
                elif not old[r["id"]] and r["passed"]:
                    fixes.append({"id": r["id"]})
    report["regressions"] = regressions
    report["fixes"] = fixes

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_path = REPORT_DIR / f"{stamp}.json"
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if not args.no_baseline:
        baseline_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + "=" * 78)
    print(f"通过 {passed}/{total}（{report['pass_rate'] * 100:.1f}%）  用时 {report['duration_s']}s  "
          f"P50 {report['latency']['p50']}ms  P95 {report['latency']['p95']}ms  "
          f"拦截 {report['blocked_count']} 条")
    for cat, v in sorted(by_cat.items()):
        print(f"  {cat:<10} {v['passed']}/{v['total']}")
    if regressions:
        print(f"\n! 相对基线退化 {len(regressions)} 条:")
        for r in regressions:
            print(f"  - {r['id']}: {r['reason']}")
    if fixes:
        print(f"\n+ 相对基线新修好 {len(fixes)} 条: {', '.join(f['id'] for f in fixes)}")
    print(f"\n报告: {out_path}")
    if not args.no_baseline:
        print(f"基线已更新: {baseline_path}")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    asyncio.run(main())
