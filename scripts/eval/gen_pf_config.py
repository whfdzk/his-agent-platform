"""从 cases_v2.json 生成 promptfoo 配置（可以只取部分切片，方便小样本验证）。

用法：
    python scripts/eval/gen_pf_config.py                       # core 全量 → promptfoo-core.yaml
    python scripts/eval/gen_pf_config.py --only kb,safety       # 只取 kb + safety
    python scripts/eval/gen_pf_config.py --ids kb-01,drug-03 --out promptfoo-smoke.yaml

生成的断言口径要和 run_eval.py 的 keyword_judge 对齐：
- expect_any         → icontains-any（promptfoo 的 contains-any 大小写敏感，自研是统一 lower）
- must_not_include   → not-icontains-any
- expect_blocked     → 把「【已拦截】」标记并进 icontains-any（provider 命中拦截时会给输出加这个前缀）
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CASES = ROOT / "scripts" / "eval" / "cases_v2.json"
OUT_DIR = ROOT / "scripts" / "eval"
BLOCK_MARK = "【已拦截】"


def q(value: object) -> str:
    """YAML 标量：复用 JSON 转义（JSON 字符串是合法 YAML 双引号标量）。"""
    return json.dumps(value, ensure_ascii=False)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", default=str(CASES))
    ap.add_argument("--split", default="core", choices=["core", "holdout", "all"])
    ap.add_argument("--only", default="", help="切片，逗号分隔，如 kb,safety")
    ap.add_argument("--ids", default="", help="只取指定用例 id，逗号分隔")
    ap.add_argument("--out", default="promptfoo-core.yaml")
    ap.add_argument("--timeout-ms", type=int, default=200000, help="单条 test 的总超时（毫秒）")
    ap.add_argument("--run-tag", default="", help="会话后缀（默认当前时间），避免复用上一批的历史会话状态")
    ap.add_argument("--skip-done", default="", help="已跑完的 jsonl：其中的用例不再生成（断点续跑）")
    ap.add_argument("--jsonl", default="", help="逐条落盘路径（相对 scripts/eval）；空则用 runs/<out>-<tag>.jsonl")
    args = ap.parse_args()
    run_tag = args.run_tag or datetime.now().strftime("%m%d%H%M")

    data = json.loads(Path(args.cases).read_text(encoding="utf-8"))
    cases = data["cases"]
    # split 口径看 holdout 标记，不看 source（source 记的是用例来源：core/curated/generated）
    if args.split == "core":
        cases = [c for c in cases if not c.get("holdout")]
    elif args.split == "holdout":
        cases = [c for c in cases if c.get("holdout")]
    if args.only:
        wanted = {s.strip() for s in args.only.split(",") if s.strip()}
        cases = [c for c in cases if c.get("slice") in wanted]
    if args.ids:
        wanted_ids = {i.strip() for i in args.ids.split(",") if i.strip()}
        cases = [c for c in cases if c.get("id") in wanted_ids]
    if args.skip_done:
        done_path = (OUT_DIR / args.skip_done) if not Path(args.skip_done).is_absolute() else Path(args.skip_done)
        done = set()
        if done_path.exists():
            for line in done_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    done.add(json.loads(line).get("id"))
                except json.JSONDecodeError:
                    continue
        cases = [c for c in cases if c["id"] not in done]
        print(f"跳过已完成 {len(done)} 条 → 剩余 {len(cases)} 条")
    if not cases:
        raise SystemExit("没有匹配到用例")

    out = OUT_DIR / args.out
    jsonl = args.jsonl or f"runs/{Path(args.out).stem}-{run_tag}.jsonl"
    lines = [
        "# 由 scripts/eval/gen_pf_config.py 生成，勿手改",
        f"description: cases_v2 {args.split}（{len(cases)} 条）",
        "prompts:",
        '  - "{{question}}"',
        "providers:",
        "  - id: file://./pf_provider.py",
        "    label: local-chat",
        "    config:",
        '      base_url: "http://127.0.0.1:8000"',
        f"      timeout: {args.timeout_ms}",
        "      timeout_ms: 90",
        f"      jsonl: {q(jsonl)}",
        "tests:",
    ]

    for c in cases:
        turns = c.get("turns") or [c["question"]]
        expect = list(c.get("expect_any") or [])
        if c.get("expect_blocked"):
            expect = [BLOCK_MARK] + expect
        lines += [
            f"  - description: {q(c['id'] + ' | ' + (c.get('slice') or ''))}",
            "    metadata:",
            f"      slice: {q(c.get('slice'))}",
            f"      kind: {q(c.get('kind'))}",
            f"      source: {q(c.get('source') or 'core')}",
            f"      expect_blocked: {str(bool(c.get('expect_blocked'))).lower()}",
            "    vars:",
            f"      case_id: {q(c['id'])}",
            f"      question: {q(c['question'])}",
            f"      session_id: {q('pf-' + c['id'] + '-' + run_tag)}",
            f"      turns_json: {q(json.dumps(turns, ensure_ascii=False))}",
            "    assert:",
        ]
        if expect:
            lines += [
                "      - type: icontains-any",
                f"        value: {json.dumps(expect, ensure_ascii=False)}",
            ]
        if c.get("must_not_include"):
            lines += [
                "      - type: not-icontains-any",
                f"        value: {json.dumps(c['must_not_include'], ensure_ascii=False)}",
            ]

    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {out.relative_to(ROOT)}（{len(cases)} 条）")


if __name__ == "__main__":
    main()
