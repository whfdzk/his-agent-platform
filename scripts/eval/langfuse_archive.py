"""把评测批次存档到 Langfuse（dataset + dataset run + 分数）。

为什么是"回放"而不是"重跑"：
    task 直接返回 promptfoo 已采集到的答案，不重新打一遍后端。重跑一次要花一遍
    token，而且答案会变（模型非确定性），存档就对不上报告里那份数字了。

数据组织：
- dataset `his-agent-eval-cases_v2`：一条用例一个 item，id 就是用例 id（kb-01…），
  expected_output 取 references_v2.json（没有 reference 的用例留空），
  metadata 带 slice / category / expect_any / expect_blocked / holdout。
- 每批一次 experiment run（run_name = 报告名），分数项：
  assertion_pass（promptfoo 断言）、faithfulness / answer_relevancy /
  context_precision / context_recall（ragas_eval.py 产出）。

用法：
    python scripts/eval/langfuse_archive.py --sync-dataset
    python scripts/eval/langfuse_archive.py --report doc/eval-reports/pf-final3-20261123.json \
        --ragas doc/eval-reports/ragas-final3-20261123.json --run-name pf-final3-20261123
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

DATASET_NAME = "his-agent-eval-cases_v2"
EXPERIMENT_NAME = "his-agent-eval"
SCORE_KEYS = ["faithfulness", "answer_relevancy", "context_precision", "context_recall",
              "context_precision_without_reference"]


def read_env() -> dict[str, str]:
    env: dict[str, str] = {}
    path = ROOT / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def make_client():
    from langfuse import Langfuse  # noqa: PLC0415

    env = read_env()
    pub = env.get("LANGFUSE_PUBLIC_KEY")
    sec = env.get("LANGFUSE_SECRET_KEY")
    host = env.get("LANGFUSE_BASE_URL") or "http://localhost:3000"
    if not pub or not sec:
        raise SystemExit("缺少 LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY（检查 .env）")
    client = Langfuse(public_key=pub, secret_key=sec, host=host)
    if not client.auth_check():
        raise SystemExit(f"Langfuse 认证失败：{host}")
    return client


def load_cases() -> list[dict]:
    return json.loads((ROOT / "scripts" / "eval" / "cases_v2.json").read_text(encoding="utf-8"))["cases"]


def sync_dataset(client, force: bool = False) -> None:
    """把 cases_v2 + references 灌进 dataset（同 id 会 upsert，重复跑安全）。

    注意 input 里必须带 case_id：Langfuse 的 evaluator 只拿得到 input，
    而 cases_v2 里有同一句问题对应多条用例的情况（按 question 对齐会丢条目，
    第一次归档 108 条只回放出 71 条就是这个原因）。
    """
    refs_path = ROOT / "scripts" / "eval" / "references_v2.json"
    refs = json.loads(refs_path.read_text(encoding="utf-8")) if refs_path.exists() else {}
    cases = load_cases()

    try:
        client.get_dataset(DATASET_NAME)
    except Exception:
        client.create_dataset(name=DATASET_NAME, description="his-agent-platform 评测用例（cases_v2）")

    for c in cases:
        client.create_dataset_item(
            dataset_name=DATASET_NAME,
            id=c["id"],
            input={"case_id": c["id"], "question": c["question"], "turns": c.get("turns") or []},
            expected_output=refs.get(c["id"]) or None,
            metadata={
                "slice": c.get("category") or c.get("slice"),
                "source": c.get("source"),
                "holdout": c.get("holdout"),
                "expect_any": c.get("expect_any"),
                "must_not_include": c.get("must_not_include"),
                "expect_blocked": c.get("expect_blocked"),
                "note": c.get("note"),
            },
        )
    client.flush()
    print(f"dataset {DATASET_NAME}: 同步 {len(cases)} 条（同 id upsert）")


def run_archive(client, report_path: Path, ragas_path: Path | None, run_name: str) -> None:
    from langfuse import Evaluation  # noqa: PLC0415

    report = json.loads(report_path.read_text(encoding="utf-8"))
    rows = report.get("results", [])

    answers: dict[str, str] = {}
    passes: dict[str, bool] = {}
    meta_by_case: dict[str, dict] = {}
    for r in rows:
        cid = r.get("id") or ""
        if not cid:
            continue
        answers[cid] = r.get("answer") or ""
        passes[cid] = bool(r.get("passed"))
        meta_by_case[cid] = {
            "latency_ms": r.get("latency_ms"),
            "citation_count": r.get("citation_count"),
            "blocked": r.get("blocked"),
            "reason": r.get("reason"),
        }

    scores: dict[str, dict] = {}
    if ragas_path and ragas_path.exists():
        ragas = json.loads(ragas_path.read_text(encoding="utf-8"))
        for d in ragas.get("details", []):
            scores[d["id"]] = {k: d.get(k) for k in SCORE_KEYS}

    # 只回放这份报告里出现过的用例（按 case_id 对齐，不按 question）
    ds = client.get_dataset(DATASET_NAME)
    items = [it for it in ds.items if (it.input or {}).get("case_id") in answers]
    if not items:
        raise SystemExit("报告里的用例在 dataset 中都找不到，先跑 --sync-dataset")
    print(f"回放 {len(items)} 条到 run {run_name}")

    def task(*, item, **kwargs):
        return answers[(item.input or {}).get("case_id") or ""]

    def assertion_evaluator(*, input, output, expected_output=None, metadata=None, **kwargs):
        cid = (input or {}).get("case_id") if isinstance(input, dict) else ""
        ok = passes.get(cid, False)
        m = meta_by_case.get(cid) or {}
        return Evaluation(
            name="assertion_pass",
            value=1 if ok else 0,
            comment=(m.get("reason") or "")[:200] or None,
            metadata=m,
            data_type="NUMERIC",
        )

    def ragas_evaluator(*, input, output, expected_output=None, metadata=None, **kwargs):
        cid = (input or {}).get("case_id") if isinstance(input, dict) else ""
        out = []
        for k, v in (scores.get(cid) or {}).items():
            if v is None:
                continue
            out.append(Evaluation(name=k, value=float(v), data_type="NUMERIC"))
        return out

    result = client.run_experiment(
        name=EXPERIMENT_NAME,
        run_name=run_name,
        description=f"{report_path.name} / {ragas_path.name if ragas_path else '无 ragas'}",
        data=items,
        task=task,
        evaluators=[assertion_evaluator, ragas_evaluator],
        max_concurrency=4,
        metadata={"report": report_path.name, "ragas": ragas_path.name if ragas_path else "",
                  "pass_rate": report.get("pass_rate")},
    )
    client.flush()
    print(f"完成：{getattr(result, 'run_name', run_name)}")
    print(f"  实验地址：{getattr(result, 'experiment_url', None) or '(未返回)'}")
    passed = 0
    total = 0
    for ir in (getattr(result, "item_results", None) or []):
        total += 1
        for ev in (getattr(ir, "evaluations", None) or []):
            if getattr(ev, "name", "") == "assertion_pass" and float(getattr(ev, "value", 0) or 0) >= 1:
                passed += 1
    print(f"  条目 {total}，断言通过 {passed}/{total}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sync-dataset", action="store_true")
    ap.add_argument("--report", default="")
    ap.add_argument("--ragas", default="")
    ap.add_argument("--run-name", default="")
    args = ap.parse_args()

    client = make_client()
    if args.sync_dataset:
        sync_dataset(client)
    if args.report:
        rp = Path(args.report)
        if not rp.is_absolute():
            rp = ROOT / rp
        rg = Path(args.ragas) if args.ragas else None
        if rg and not rg.is_absolute():
            rg = ROOT / rg
        run_archive(client, rp, rg, args.run_name or rp.stem)
    client.shutdown()


if __name__ == "__main__":
    main()
