"""只读端点并发压测 + 对话链路小并发时延。

输出：doc/eval-reports/loadtest-<date>.json

用法：
  python tools/verify/load_test.py [--concurrency 20 50] [--duration 15] [--chat 3]

说明：
- 只读压测不触达大模型，打的是 health / 会话 / 预约 / 概览 / me；
- 对话压测只发少量请求（默认 3 条），单独统计首字节与整轮耗时，避免烧 token；
- 后端当前是单进程单 worker（uvicorn --workers 1），结果对应该部署形态。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import statistics
import time
from datetime import datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
PHONE = os.getenv("EVAL_PHONE", "13800138001")
PASSWORD = os.getenv("EVAL_PASSWORD", "test123456")


def pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round((p / 100) * len(ordered) - 0.5))))
    return round(ordered[idx], 1)


def summarize(name: str, latencies: list[float], codes: dict[str, int], wall: float) -> dict:
    ok = sum(v for k, v in codes.items() if k.startswith("2"))
    total = sum(codes.values())
    return {
        "scenario": name,
        "requests": total,
        "ok": ok,
        "errors": total - ok,
        "error_rate": round((total - ok) / total, 4) if total else 0.0,
        "qps": round(total / wall, 2) if wall else 0.0,
        "latency_ms": {
            "mean": round(statistics.fmean(latencies), 1) if latencies else 0.0,
            "p50": pct(latencies, 50),
            "p95": pct(latencies, 95),
            "p99": pct(latencies, 99),
            "max": round(max(latencies), 1) if latencies else 0.0,
        },
        "codes": codes,
    }


async def _login(client: httpx.AsyncClient) -> str:
    r = await client.post("/api/v1/auth/login", json={"phone": PHONE, "password": PASSWORD})
    r.raise_for_status()
    return r.json()["token"]


async def readonly_run(base: str, concurrency: int, duration: float, token: str) -> dict:
    limits = httpx.Limits(max_connections=concurrency * 2, max_keepalive_connections=concurrency)
    async with httpx.AsyncClient(base_url=base, timeout=20.0, limits=limits, trust_env=False) as client:
        headers = {"Authorization": f"Bearer {token}"}
        endpoints = [
            ("/health", {}),
            ("/api/v1/sessions", headers),
            ("/api/v1/appointments", headers),
            ("/api/v1/patient/overview", headers),
            ("/api/v1/auth/me", headers),
        ]
        # 预热，避免把首连握手算进 P95
        for path, h in endpoints:
            await client.get(path, headers=h)

        latencies: list[float] = []
        codes: dict[str, int] = {}
        stop_at = time.perf_counter() + duration

        async def worker() -> None:
            while time.perf_counter() < stop_at:
                path, h = random.choice(endpoints)
                t0 = time.perf_counter()
                try:
                    r = await client.get(path, headers=h)
                    code = str(r.status_code)
                except Exception as exc:  # noqa: BLE001 - 压测要把异常计入错误率
                    code = type(exc).__name__
                latencies.append((time.perf_counter() - t0) * 1000)
                codes[code] = codes.get(code, 0) + 1

        t0 = time.perf_counter()
        await asyncio.gather(*(worker() for _ in range(concurrency)))
        wall = time.perf_counter() - t0
        return summarize(f"readonly_c{concurrency}", latencies, codes, wall)


async def chat_run(base: str, count: int, token: str) -> dict:
    async with httpx.AsyncClient(base_url=base, timeout=240.0, trust_env=False) as client:
        headers = {"Authorization": f"Bearer {token}"}
        ttfb: list[float] = []
        total: list[float] = []
        codes: dict[str, int] = {}

        async def one(i: int) -> None:
            sid = f"load-{int(time.time())}-{i}"
            payload = {"session_id": sid, "message": "你好"}
            t0 = time.perf_counter()
            first = None
            try:
                async with client.stream("POST", "/api/v1/chat/stream", headers=headers, json=payload) as resp:
                    if resp.status_code != 200:
                        codes[str(resp.status_code)] = codes.get(str(resp.status_code), 0) + 1
                        total.append((time.perf_counter() - t0) * 1000)
                        return
                    async for line in resp.aiter_lines():
                        if first is None and line.startswith("data:"):
                            first = (time.perf_counter() - t0) * 1000
                        if line.strip() == "data: [DONE]":
                            break
                codes["200"] = codes.get("200", 0) + 1
            except Exception as exc:  # noqa: BLE001
                codes[type(exc).__name__] = codes.get(type(exc).__name__, 0) + 1
            total.append((time.perf_counter() - t0) * 1000)
            ttfb.append(first if first is not None else total[-1])

        t0 = time.perf_counter()
        await asyncio.gather(*(one(i) for i in range(count)))
        wall = time.perf_counter() - t0
        result = summarize(f"chat_c{count}", total, codes, wall)
        result["ttfb_ms"] = {
            "p50": pct(ttfb, 50),
            "p95": pct(ttfb, 95),
            "max": round(max(ttfb), 1) if ttfb else 0.0,
        }
        return result


async def main_async(args) -> dict:
    report = {
        "base": args.base,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "scenarios": [],
        "notes": "后端为单进程 uvicorn --workers 1；只读场景不含大模型调用。",
    }
    # 登录限流是 5 次 / 300s（账号 + IP 双维度），整轮只登录一次并复用 token
    async with httpx.AsyncClient(base_url=args.base, timeout=20.0, trust_env=False) as client:
        token = await _login(client)
    for c in args.concurrency:
        report["scenarios"].append(await readonly_run(args.base, c, args.duration, token))
    if args.chat > 0:
        report["scenarios"].append(await chat_run(args.base, args.chat, token))
    return report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.getenv("E2E_BASE", "http://127.0.0.1:8000"))
    ap.add_argument("--concurrency", type=int, nargs="+", default=[20, 50])
    ap.add_argument("--duration", type=float, default=15.0)
    ap.add_argument("--chat", type=int, default=3, help="对话并发条数，0 表示跳过")
    args = ap.parse_args()

    report = asyncio.run(main_async(args))
    out = ROOT / "doc" / "eval-reports" / f"loadtest-{datetime.now():%Y%m%d}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    for s in report["scenarios"]:
        lat = s["latency_ms"]
        extra = ""
        if "ttfb_ms" in s:
            extra = f" ttfb_p95={s['ttfb_ms']['p95']}ms"
        print(
            f"{s['scenario']:<14} req={s['requests']:<6} ok={s['ok']:<6} "
            f"err={s['error_rate']:.2%} qps={s['qps']:<8} "
            f"p50={lat['p50']}ms p95={lat['p95']}ms p99={lat['p99']}ms{extra}"
        )
    print(f"\n报告：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
