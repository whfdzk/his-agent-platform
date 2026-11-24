"""把 patch 报告里同 id 的用例结果覆盖到 base 报告，重算总数/切片/Wilson CI。

用途：全片跑批里个别用例因环境抖动失败（超时/网关），单条复跑通过后，
用这条「复跑确认」覆盖原报告，得到一份可直接接门禁的完整报告，避免整片重跑。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from refresh_baseline import wilson  # noqa: E402


def main() -> None:
    base_path, patch_path, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
    base = json.loads(Path(base_path).read_text(encoding="utf-8"))
    patch = json.loads(Path(patch_path).read_text(encoding="utf-8"))

    rows = {r["id"]: r for r in base["results"]}
    replaced = []
    for r in patch["results"]:
        if r["id"] in rows:
            rows[r["id"]] = r
            replaced.append(r["id"])
        else:
            print(f"! patch 里的 {r['id']} 不在 base，忽略")

    base["results"] = list(rows.values())
    total = len(base["results"])
    passed = sum(1 for r in base["results"] if r.get("passed"))
    base["total"] = total
    base["passed"] = passed
    base["pass_rate"] = round(passed / total, 3) if total else 0.0
    base["pass_rate_ci95"] = wilson(passed, total)

    by_slice: dict[str, dict] = {}
    for r in base["results"]:
        name = r.get("slice") or r.get("category") or "unknown"
        blk = by_slice.setdefault(name, {"total": 0, "passed": 0})
        blk["total"] += 1
        blk["passed"] += 1 if r.get("passed") else 0
    for blk in by_slice.values():
        blk["pass_rate"] = round(blk["passed"] / blk["total"], 3)
        blk["ci95"] = wilson(blk["passed"], blk["total"])
    base["by_slice"] = by_slice

    Path(out_path).write_text(json.dumps(base, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"覆盖 {len(replaced)} 条：{', '.join(replaced)}")
    print(f"合并后 {passed}/{total} = {base['pass_rate']:.1%}  CI {base['pass_rate_ci95']}")
    print(f"写入 {out_path}")


if __name__ == "__main__":
    main()
