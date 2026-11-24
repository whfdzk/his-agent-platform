"""用一次 promptfoo 跑批结果刷新基线里的某个切片。

为什么需要：基线是"改造前快照"，安全切片的旧答案已被证实是历史错误
（safety-04 当时连求助渠道都没给）。门禁拿这份基线自检会一直红，
但红的原因不是当前代码退化，而是基线本身过期。刷新时必须留痕：
写清来源报告、刷新时间、替换了哪些 id，不能悄悄改数字。

用法：
    python scripts/eval/refresh_baseline.py --slice safety \
        --from doc/eval-reports/pf-safety-core-safety5.json
"""
from __future__ import annotations

import argparse
import json
import math
import shutil
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BASELINE = ROOT / "doc" / "eval-reports" / "baseline-cases_v2-core.json"


def wilson(passed: int, total: int, z: float = 1.96) -> list[float]:
    if total == 0:
        return [0.0, 0.0]
    p = passed / total
    denom = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denom
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denom
    return [round(max(0.0, center - half), 3), round(min(1.0, center + half), 3)]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slice", required=True)
    ap.add_argument("--from", dest="src", required=True, help="promptfoo 转出的报告（pf_to_report 产物）")
    ap.add_argument("--baseline", default=str(DEFAULT_BASELINE))
    ap.add_argument("--backup-dir", default=str(ROOT / "doc" / "eval-reports" / "_archive"))
    args = ap.parse_args()

    base_path = Path(args.baseline).resolve()
    src_path = Path(args.src).resolve()
    base = json.loads(base_path.read_text(encoding="utf-8"))
    src = json.loads(src_path.read_text(encoding="utf-8"))

    new_rows = {r["id"]: r for r in src.get("results", []) if r.get("slice") == args.slice}
    if not new_rows:
        raise SystemExit(f"{src_path} 里没有 slice={args.slice} 的结果")
    unknown = set(new_rows) - {r["id"] for r in base["results"]}
    if unknown:
        raise SystemExit(f"这些 id 不在基线里，拒绝静默新增：{sorted(unknown)}")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = Path(args.backup_dir) / f"{stamp}-before-{args.slice}-refresh"
    backup.mkdir(parents=True, exist_ok=True)
    shutil.copy2(base_path, backup / base_path.name)

    replaced: list[str] = []
    for row in base["results"]:
        if row.get("slice") != args.slice or row["id"] not in new_rows:
            continue
        new = new_rows[row["id"]]
        old_passed = row.get("passed")
        row.update({
            "passed": new.get("passed"),
            "reason": new.get("reason", ""),
            "answer": new.get("answer", ""),
            "latency_ms": new.get("latency_ms"),
            "chars": new.get("chars"),
            "blocked": new.get("blocked", False),
            "busy": new.get("busy", False),
            "error": new.get("error", ""),
            "question": new.get("question", row.get("question")),
            "refreshed_from": str(src_path.relative_to(ROOT)),
            "refreshed_at": stamp,
            "passed_before_refresh": old_passed,
        })
        replaced.append(row["id"])

    total = len(base["results"])
    passed = sum(1 for r in base["results"] if r.get("passed"))
    base["total"] = total
    base["passed"] = passed
    base["pass_rate"] = round(passed / total, 3) if total else 0.0
    base["pass_rate_ci95"] = wilson(passed, total)

    for name in {r.get("slice") for r in base["results"]}:
        rows = [r for r in base["results"] if r.get("slice") == name]
        ok = sum(1 for r in rows if r.get("passed"))
        base.setdefault("by_slice", {})[name] = {
            "total": len(rows),
            "passed": ok,
            "pass_rate": round(ok / len(rows), 3) if rows else 0.0,
            "ci95": wilson(ok, len(rows)),
        }

    base["slices_refreshed"] = {
        args.slice: {
            "from": str(src_path.relative_to(ROOT)),
            "at": stamp,
            "ids": sorted(replaced),
            "source_pass_rate": src.get("pass_rate"),
        }
    }
    base_path.write_text(json.dumps(base, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"刷新 {args.slice}：{len(replaced)} 条 → {base['by_slice'][args.slice]}")
    print(f"全量：{passed}/{total} = {base['pass_rate']:.1%}  CI {base['pass_rate_ci95']}")
    print(f"原基线已备份到 {backup}")


if __name__ == "__main__":
    main()
