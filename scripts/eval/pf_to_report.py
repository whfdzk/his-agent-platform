"""把 promptfoo 的输出 json 转成 run_eval.py 同款报告结构，喂给 gate.py。

为什么要这一层：promptfoo 判"这条断言过没过"，我们的门禁判"哪个切片退了、安全项破没破 100%"，
两者数据模型不同；转换保持 ~40 行，gate.py 不用改，Wilson CI 也还用自研那套。

用法：
    python scripts/eval/pf_to_report.py scripts/eval/pf-eval8.json
    python scripts/eval/gate.py --current doc/eval-reports/pf-<run>.json
"""
from __future__ import annotations

import json
import math
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def wilson(passed: int, total: int, z: float = 1.96) -> tuple[float, float]:
    if total == 0:
        return (0.0, 0.0)
    p = passed / total
    denom = 1 + z * z / total
    centre = p + z * z / (2 * total)
    spread = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total)
    return ((centre - spread) / denom, (centre + spread) / denom)


def pct(values: list[int], p: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round((p / 100) * (len(ordered) - 1)))))
    return ordered[idx]


def main() -> None:
    pf_path = Path(sys.argv[1])
    out_path = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / "doc" / "eval-reports" / (
        f"pf-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    )

    pf = json.loads(pf_path.read_text(encoding="utf-8"))
    rows = []
    for r in pf["results"]["results"]:
        test = r.get("testCase") or {}
        desc = (test.get("description") or "").split(" | ")[0]
        meta = test.get("metadata") or {}
        resp_meta = (r.get("response") or {}).get("metadata") or {}
        output = (r.get("response") or {}).get("output") or ""
        rows.append(
            {
                "id": desc,
                "slice": meta.get("slice"),
                "kind": meta.get("kind"),
                "passed": bool((r.get("gradingResult") or {}).get("pass")),
                "blocked": bool(resp_meta.get("blocked")),
                "busy": bool(resp_meta.get("busy")),
                "error": r.get("error") or (resp_meta.get("error") or ""),
                "latency_ms": int(r.get("latencyMs") or 0),
                "chars": len(output),
                "question": (test.get("vars") or {}).get("question") or "",
                "answer": output,
                "reason": ((r.get("gradingResult") or {}).get("reason") or "")[:120],
                "citations": resp_meta.get("citations") or [],
                "citation_count": len(resp_meta.get("citations") or []),
            }
        )

    total = len(rows)
    passed = sum(1 for r in rows if r["passed"])
    by_slice: dict[str, dict] = {}
    for r in rows:
        key = r["slice"] or "unknown"
        g = by_slice.setdefault(key, {"total": 0, "passed": 0})
        g["total"] += 1
        g["passed"] += 1 if r["passed"] else 0
    for key, g in by_slice.items():
        lo, hi = wilson(g["passed"], g["total"])
        g["pass_rate"] = round(g["passed"] / g["total"], 3) if g["total"] else 0
        g["ci95"] = [round(lo, 3), round(hi, 3)]

    adv = [r for r in rows if r["slice"] == "adversarial"]
    benign = [r for r in rows if r["slice"] not in ("adversarial", "safety")]
    adv_blocked = sum(1 for r in adv if r["blocked"])
    benign_blocked = sum(1 for r in benign if r["blocked"])

    latencies = [r["latency_ms"] for r in rows if r["latency_ms"]]
    lo, hi = wilson(passed, total)
    report = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "cases_path": str(pf_path),
        "runner": "promptfoo",
        "eval_id": pf.get("evalId"),
        "total": total,
        "passed": passed,
        "pass_rate": round(passed / total, 3) if total else 0,
        "pass_rate_ci95": [round(lo, 3), round(hi, 3)],
        "by_slice": by_slice,
        "latency": {"p50": pct(latencies, 50), "p95": pct(latencies, 95), "max": max(latencies or [0])},
        "blocked_count": sum(1 for r in rows if r["blocked"]),
        "busy_count": sum(1 for r in rows if r["busy"]),
        "avg_chars": round(sum(r["chars"] for r in rows) / total, 1) if total else 0,
        "adversarial": {
            "total": len(adv),
            "blocked": adv_blocked,
            "block_rate": round(adv_blocked / len(adv), 3) if adv else None,
        },
        "false_positive": {
            "total": len(benign),
            "blocked": benign_blocked,
            "rate": round(benign_blocked / len(benign), 3) if benign else None,
        },
        "results": rows,
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"{total} 条：通过 {passed}（{report['pass_rate']:.1%}，CI {lo:.1%}~{hi:.1%}）")
    for key, g in sorted(by_slice.items()):
        print(f"  {key:12} {g['passed']}/{g['total']} = {g['pass_rate']:.1%}")
    print(f"报告写入 {out_path}")


if __name__ == "__main__":
    main()
