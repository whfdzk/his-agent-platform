"""跑批薄壳：调 promptfoo 执行，断点续跑，逐条落盘。

分工不变：执行与断言仍然由 promptfoo 做，本脚本只负责
1) 每轮把「还没跑完的用例」生成一个临时配置；
2) 跑完后从 jsonl 统计进度，接着补没跑完的；
3) 全部跑完后把各轮输出合并成一份 promptfoo 同构 json，交给 pf_to_report.py。

为什么需要它：promptfoo 自己的 -o 只在整批结束时写文件，中途被打断就全丢。
pf_provider 现在每完成一条就往 runs/<stem>-<tag>.jsonl 追加一行，
所以中断后重跑同一条命令即可续上，已完成的用例不会重跑。

用法：
    python scripts/eval/pf_run.py --split core --only safety --tag safety2
    python scripts/eval/pf_run.py --split core --only kb,drug --tag kd2 --report doc/eval-reports/pf-kb-drug-20261124.json
"""
from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EVAL_DIR = ROOT / "scripts" / "eval"
CASES = EVAL_DIR / "cases_v2.json"
RUNS = EVAL_DIR / "runs"
PYTHON = sys.executable


def load_cases(split: str, only: set[str], ids: set[str]) -> list[dict]:
    data = json.loads(CASES.read_text(encoding="utf-8"))
    cases = data["cases"]
    if split == "core":
        cases = [c for c in cases if not c.get("holdout")]
    elif split == "holdout":
        cases = [c for c in cases if c.get("holdout")]
    if only:
        cases = [c for c in cases if c.get("slice") in only]
    if ids:
        cases = [c for c in cases if c.get("id") in ids]
    return cases


def pick_sample(cases: list[dict], n: int, seed: int,
                full_slices: set[str] | None = None) -> list[dict]:
    """按切片分层抽样：full_slices 里的切片全量保留，其余切片合计抽 n 条。

    safety 是门禁硬项（必须 100%），抽样绿了也说明不了问题，默认整片全跑；
    kb/triage/drug 这类切片量大、单条成本一样，才用抽样控 token。
    分层是为了避免随机抽到全是 kb、安全用例一条没抽中的情况——
    那样报告里的切片对比会失真。同 seed 同输入结果稳定，可复现。
    """
    full_slices = full_slices or set()
    kept = [c for c in cases if (c.get("slice") or "all") in full_slices]
    rest = [c for c in cases if (c.get("slice") or "all") not in full_slices]
    if not rest:
        print(f"全量切片 {sorted(full_slices)} 已覆盖全部用例，不再抽样")
        return kept
    if n <= 0 or n >= len(rest):
        print(f"全量切片 {sorted(full_slices)} 保留 {len(kept)} 条；其余 {len(rest)} 条全跑")
        return kept + rest

    groups: dict[str, list[dict]] = {}
    for c in rest:
        groups.setdefault(c.get("slice") or "all", []).append(c)
    order = sorted(groups)
    if n <= len(order):
        # 比切片数还少时，优先挑条目最多的那几个切片，各 1 条
        order = sorted(order, key=lambda k: -len(groups[k]))[:n]
        picked = [sorted(groups[k], key=lambda c: c["id"])[0] for k in order]
        print(_dist_note({k: 1 for k in order}, groups, kept, full_slices))
        return kept + picked

    alloc = {k: 1 for k in order}
    left = n - len(order)
    total = len(rest)
    for k in sorted(order, key=lambda k: -len(groups[k])):
        extra = int(left * len(groups[k]) / total)
        cap = len(groups[k]) - 1
        alloc[k] += min(extra, cap)
    picked_n = sum(alloc.values())
    # 比例取整的余数补给最大的切片
    while picked_n < n:
        for k in sorted(order, key=lambda k: -len(groups[k])):
            if alloc[k] < len(groups[k]):
                alloc[k] += 1
                picked_n += 1
                if picked_n == n:
                    break
    while picked_n > n:
        for k in sorted(order, key=lambda k: -len(groups[k])):
            if alloc[k] > 1:
                alloc[k] -= 1
                picked_n -= 1
                if picked_n == n:
                    break

    rng = random.Random(seed)
    picked = []
    for k in order:
        picked += rng.sample(sorted(groups[k], key=lambda c: c["id"]), alloc[k])
    print(_dist_note(alloc, groups, kept, full_slices))
    return kept + picked


def _dist_note(alloc: dict[str, int], groups: dict[str, list[dict]],
               kept: list[dict], full_slices: set[str]) -> str:
    parts = []
    for k in sorted(full_slices):
        total_k = len([c for c in kept if (c.get("slice") or "all") == k])
        if total_k:
            parts.append(f"{k} {total_k}/{total_k}(全量)")
    for k in sorted(alloc):
        parts.append(f"{k} {alloc[k]}/{len(groups[k])}")
    return "采样分布：" + "、".join(parts)


def done_ids(jsonl: Path) -> set[str]:
    out: set[str] = set()
    if not jsonl.exists():
        return out
    for line in jsonl.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rid = json.loads(line).get("id")
        except json.JSONDecodeError:
            continue
        if rid:
            out.add(rid)
    return out


def merge_parts(parts: list[Path], out: Path) -> None:
    merged: dict = {}
    rows: list = []
    for p in parts:
        if not p.exists():
            continue
        d = json.loads(p.read_text(encoding="utf-8"))
        if not merged:
            merged = {k: v for k, v in d.items() if k != "results"}
        rows += (d.get("results") or {}).get("results") or []
    merged["results"] = {"results": rows}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"合并 {len(parts)} 份输出 → {out.relative_to(ROOT)}（{len(rows)} 条）")


def login(base: str, phone: str, password: str) -> str:
    """拿评测账号 token：匿名请求走 persist=False 分支，测出来的行为与真实用户不一致。"""
    import urllib.request

    payload = json.dumps({"phone": phone, "password": password}).encode("utf-8")
    req = urllib.request.Request(
        f"{base}/api/v1/auth/login",
        data=payload,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))["token"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="core", choices=["core", "holdout", "all"])
    ap.add_argument("--only", default="")
    ap.add_argument("--ids", default="")
    ap.add_argument("--tag", required=True, help="批次标签，决定 session_id 后缀与落盘文件名")
    ap.add_argument("--out", default="", help="合并后的 promptfoo json（默认 <stem>.json）")
    ap.add_argument("--report", default="", help="顺便转成 gate 能读的报告")
    ap.add_argument("--sample", type=int, default=8,
                    help="非门禁切片合计抽 N 条（默认 8）；传 0 才是全量")
    ap.add_argument("--full-slices", default="safety",
                    help="这些切片整片全跑、不参与抽样（默认 safety，门禁硬项）；传空串关闭")
    ap.add_argument("--seed", type=int, default=42, help="采样种子，同 seed 结果可复现")
    ap.add_argument("--dry-run", action="store_true", help="只生成配置并打印，不真正跑（不花 token）")
    ap.add_argument("--timeout-ms", type=int, default=200000)
    ap.add_argument("--max-rounds", type=int, default=6)
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    only = {s.strip() for s in args.only.split(",") if s.strip()}
    ids = {i.strip() for i in args.ids.split(",") if i.strip()}
    full = load_cases(args.split, only, ids)
    if not full:
        raise SystemExit("没有匹配到用例")

    sampled = 0 < args.sample < len(full)
    full_slices = {s.strip() for s in args.full_slices.split(",") if s.strip()}
    cases = pick_sample(full, args.sample, args.seed, full_slices) if sampled else full
    print(f"本批 {len(cases)}/{len(full)} 条（--sample {args.sample}，传 0 才是全量；"
          f"全量切片 {sorted(full_slices) or '无'}）")

    stem = f"pf-{'-'.join(sorted(only)) or 'all'}-{args.split}-{args.tag}" if only else f"pf-{args.split}-{args.tag}"
    if sampled and len(cases) < len(full):
        stem += f"-s{args.sample}-seed{args.seed}"
    jsonl = RUNS / f"{stem}.jsonl"
    yaml_path = EVAL_DIR / f"{stem}.yaml"
    out_json = Path(args.out) if args.out else EVAL_DIR / f"{stem}.json"
    jsonl.parent.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    env.setdefault("PROMPTFOO_PYTHON", PYTHON)
    env.setdefault("PROMPTFOO_DISABLE_TELEMETRY", "1")
    base = env.get("EVAL_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
    if args.dry_run:
        print("dry-run：跳过登录与执行，只生成配置")
    else:
        try:
            token = env.get("EVAL_TOKEN") or login(
                base,
                env.get("EVAL_PHONE", "13800138001"),
                env.get("EVAL_PASSWORD", "test123456"),
            )
            env["EVAL_TOKEN"] = token
            print(f"已获取评测账号 token（{base}）")
        except Exception as e:  # 登录失败就退回匿名，但要让人看见
            print(f"! 登录失败，将以匿名身份跑批（handoff 等行为与真实用户不一致）: {type(e).__name__}: {e}")
    probe = {"runs": 0, "parts": []}

    for rnd in range(1, args.max_rounds + 1):
        done = done_ids(jsonl)
        remaining = [c for c in cases if c["id"] not in done]
        if not remaining:
            break
        print(f"[第 {rnd} 轮] 已完成 {len(done)}/{len(cases)}，待跑 {len(remaining)} 条")

        subprocess.run(
            [
                PYTHON, str(EVAL_DIR / "gen_pf_config.py"),
                "--split", args.split,
                "--ids", ",".join(c["id"] for c in remaining),
                "--out", yaml_path.name,
                "--run-tag", args.tag,
                "--jsonl", str(jsonl.relative_to(EVAL_DIR)),
                "--timeout-ms", str(args.timeout_ms),
            ],
            cwd=str(ROOT), check=True,
        )
        if args.dry_run:
            print(f"dry-run：配置 {yaml_path.name} 已生成（{len(remaining)} 条），未调用 promptfoo")
            return

        part = RUNS / f"{stem}.part{rnd}.json"
        pf_cmd = [
            "npx", "--yes", "promptfoo@0.126.0", "eval",
            "-c", str(yaml_path.relative_to(ROOT)), "--no-cache", "--no-progress-bar",
            "-j", "1", "-o", str(part.relative_to(ROOT)),
        ]
        if os.name == "nt":
            # npx 是 .cmd，Windows 上 subprocess 必须经 cmd /c 才能执行
            pf_cmd = ["cmd", "/c"] + pf_cmd
        rc = subprocess.run(pf_cmd, cwd=str(ROOT), env=env).returncode
        probe["parts"].append(part)
        print(f"  promptfoo exit={rc}（100 表示有断言失败，属正常）")

        after = done_ids(jsonl)
        if len(after) <= len(done):
            print("  本轮没有任何新增落盘，重试一次")
        probe["runs"] += 1

    done = done_ids(jsonl)
    print(f"结束：{len(done)}/{len(cases)} 条已落盘")

    if len(done) < len(cases):
        missing = [c["id"] for c in cases if c["id"] not in done]
        print("未跑完：" + ", ".join(missing[:20]) + (" …" if len(missing) > 20 else ""))
        print(f"再跑一次同样的命令可续跑（已完成条目不会重复执行）")
        raise SystemExit(2)

    merge_parts(probe["parts"], out_json)
    if args.report:
        subprocess.run([PYTHON, str(EVAL_DIR / "pf_to_report.py"), str(out_json), args.report],
                       cwd=str(ROOT), check=True)


if __name__ == "__main__":
    main()
