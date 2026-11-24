"""Ragas 四指标评测（RAG 三元组 + 生成质量），替掉自研那套 bigram 覆盖率。

四个指标全部用 Ragas 现成的：
- faithfulness       答案里的陈述能不能被检索原文支撑
- answer_relevancy   答案是否切题（需要 embedding 算相似度）
- context_precision  检索片段里有多少是真正有用且排在前面（需要 reference）
- context_recall     标准答案的关键点是否都被检索回来（需要 reference）

contexts 取自**真实检索结果**（进程内调 search_docs_raw 拿 hits[].text），
不是 SSE citation 里那段 ≤400 字的 quote —— 用截断引用当上下文会把分数压成假的低。

没有 reference 的用例只跑 faithfulness + answer_relevancy + context_precision_without_reference，
并在报告的 skipped_reference 里列出来，不参与 precision/recall 的均值。

踩过的坑（避免重复）：
1. llm_factory 的 Instructor 路径对 DashScope 会卡死，用 LangchainLLMWrapper(ChatOpenAI)；
2. 必须 extra_body={"enable_thinking": False}，否则输出预算被推理 token 吃光，报 LLMDidNotFinishException；
3. AnswerRelevancy 会请求 3 个 generations，DashScope 只回 1 个，Ragas 会打一行 warning 但不影响结果。

用法：
    python scripts/eval/ragas_eval.py --report doc/eval-reports/pf-kb-drug10-20261123.json --only kb,drug
    python scripts/eval/ragas_eval.py --report ... --ids kb-01,drug-01 --concurrency 2
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import warnings
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def read_env() -> dict:
    env: dict[str, str] = {}
    env_path = ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


async def retrieve_contexts(question: str, doc_type: str | None) -> list[str]:
    """进程内直连检索，复刻模型实际看到的那段证据。

    关键：不能直接拿 hits[].text 全量当 contexts。线上 fusion 是
    format_doc_context(hits)[:CHANNEL_SNIPPET_LIMIT] 再交给模型，
    超出 2200 字的 chunk 模型根本没看见，算 recall 时不能算它已提供。
    """
    from src.knowledge.deps import build_knowledge_deps  # noqa: PLC0415
    from src.knowledge.doc_rag import format_doc_context, search_docs_raw  # noqa: PLC0415
    from src.knowledge.fusion import CHANNEL_SNIPPET_LIMIT  # noqa: PLC0415

    deps = await build_knowledge_deps()
    if deps.embedding_model is None or deps.milvus_client is None:
        raise RuntimeError("知识库依赖不可用（embedding/milvus）")
    hits = await search_docs_raw(
        question=question,
        embedding_model=deps.embedding_model,
        milvus_client=deps.milvus_client,
        doc_type=doc_type,
    )
    seen = format_doc_context(hits)[:CHANNEL_SNIPPET_LIMIT]
    if not seen:
        return []
    # 按 fusion 的分隔符切开，还原成模型眼里的一个个片段
    return [c.strip() for c in seen.split("\n\n---\n\n") if c.strip()]


async def main_async(args) -> None:
    from langchain_openai import ChatOpenAI  # noqa: PLC0415
    from ragas import EvaluationDataset, evaluate  # noqa: PLC0415
    from ragas.embeddings import LangchainEmbeddingsWrapper  # noqa: PLC0415
    from ragas.llms import LangchainLLMWrapper  # noqa: PLC0415
    from ragas.metrics import (  # noqa: PLC0415
        AnswerRelevancy,
        ContextPrecision,
        ContextRecall,
        Faithfulness,
    )
    from ragas.run_config import RunConfig  # noqa: PLC0415

    env = read_env()
    chat = ChatOpenAI(
        base_url=env.get("BASE_URL_CHAT", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
        api_key=env.get("DASHSCOPE_API_KEY", ""),
        model=env.get("CHAT_MODEL", "qwen3.8-flash"),
        temperature=0,
        timeout=90,
        extra_body={"enable_thinking": False},
    )
    llm = LangchainLLMWrapper(chat)

    from src.infra.embedding import get_embeddings  # noqa: PLC0415

    emb = LangchainEmbeddingsWrapper(get_embeddings())

    report = json.loads(Path(args.report).read_text(encoding="utf-8"))
    refs = json.loads(Path(args.references).read_text(encoding="utf-8"))
    slices = {s.strip() for s in args.only.split(",") if s.strip()} if args.only else None
    ids = {i.strip() for i in args.ids.split(",") if i.strip()} if args.ids else None

    rows = []
    for r in report.get("results", []):
        if slices and r.get("slice") not in slices:
            continue
        if ids and r.get("id") not in ids:
            continue
        if not r.get("answer") or r.get("blocked"):
            continue
        question = r.get("question") or ""
        if not question:
            continue
        doc_type = "drug_instruction" if (not args.no_drug_filter) and r.get("slice") == "drug" else None
        try:
            contexts = await retrieve_contexts(question, doc_type)
        except Exception as e:  # 检索失败就跳过这条，不污染均值
            print(f"  {r['id']:<10} 检索失败，跳过：{type(e).__name__}: {e}", flush=True)
            continue
        if not contexts:
            print(f"  {r['id']:<10} 检索空结果，跳过", flush=True)
            continue
        rows.append(
            {
                "id": r["id"],
                "slice": r.get("slice"),
                "user_input": question,
                "response": r["answer"],
                "retrieved_contexts": contexts[: args.max_contexts],
                "reference": refs.get(r["id"], ""),
            }
        )
    if args.limit:
        rows = rows[: args.limit]
    if not rows:
        raise SystemExit("没有可用样本")

    with_ref = [x for x in rows if x["reference"]]
    no_ref = [x for x in rows if not x["reference"]]
    print(f"样本 {len(rows)} 条：{len(with_ref)} 条有 reference（跑四指标），{len(no_ref)} 条无 reference（跑三指标）", flush=True)

    run_config = RunConfig(max_workers=max(1, args.concurrency), timeout=180)

    def to_records(result) -> dict[str, dict]:
        df = result.to_pandas()
        out = {}
        for _, row in df.iterrows():
            rec = {k: (None if row[k] != row[k] else float(row[k])) for k in df.columns if k not in
                   ("user_input", "response", "retrieved_contexts", "reference")}
            out[row["user_input"]] = rec
        return out

    scores: dict[str, dict] = {}
    if with_ref:
        ds = EvaluationDataset.from_list(with_ref)
        res = evaluate(
            ds,
            metrics=[
                Faithfulness(llm=llm),
                AnswerRelevancy(llm=llm, embeddings=emb),
                ContextPrecision(llm=llm),
                ContextRecall(llm=llm),
            ],
            llm=llm,
            embeddings=emb,
            run_config=run_config,
            raise_exceptions=False,
            show_progress=False,
        )
        for q, rec in to_records(res).items():
            scores[q] = rec
    if no_ref:
        ds2 = EvaluationDataset.from_list(no_ref)
        from ragas.metrics import ContextPrecisionWithoutReference  # noqa: PLC0415

        res2 = evaluate(
            ds2,
            metrics=[
                Faithfulness(llm=llm),
                AnswerRelevancy(llm=llm, embeddings=emb),
                ContextPrecisionWithoutReference(llm=llm),
            ],
            llm=llm,
            embeddings=emb,
            run_config=run_config,
            raise_exceptions=False,
            show_progress=False,
        )
        for q, rec in to_records(res2).items():
            scores.setdefault(q, {}).update(rec)

    metric_keys = ["faithfulness", "answer_relevancy", "context_precision", "context_recall",
                   "context_precision_without_reference"]
    out_rows = []
    for x in rows:
        rec = scores.get(x["user_input"], {})
        row = {"id": x["id"], "slice": x["slice"], "contexts": len(x["retrieved_contexts"]),
               "has_reference": bool(x["reference"])}
        for k in metric_keys:
            if k in rec:
                row[k] = None if rec[k] is None else round(rec[k], 3)
        out_rows.append(row)

    def mean(key: str) -> float | None:
        vals = [r[key] for r in out_rows if r.get(key) is not None]
        return round(sum(vals) / len(vals), 3) if vals else None

    by_slice: dict[str, dict] = {}
    for r in out_rows:
        g = by_slice.setdefault(r["slice"] or "unknown", {"n": 0})
        g["n"] += 1
        for k in metric_keys:
            if r.get(k) is not None:
                g.setdefault(k, []).append(r[k])
    for g in by_slice.values():
        for k, v in list(g.items()):
            if isinstance(v, list):
                g[k] = round(sum(v) / len(v), 3)

    summary = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "report": str(args.report),
        "framework": "ragas",
        "ragas_slices": args.only,
        "rows": len(out_rows),
        "with_reference": len(with_ref),
        "without_reference": len(no_ref),
        "aggregate": {k: mean(k) for k in metric_keys},
        "by_slice": by_slice,
        "details": out_rows,
    }
    out_path = Path(args.out) if args.out else ROOT / "doc" / "eval-reports" / (
        f"ragas-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    )
    out_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print()
    for r in out_rows:
        print("  {:<10} slice={:<6} ctx={:<3} faith={} relev={} ctx_prec={} ctx_rec={}".format(
            r["id"], r["slice"], r["contexts"], r.get("faithfulness"), r.get("answer_relevancy"),
            r.get("context_precision") if r.get("context_precision") is not None else r.get("context_precision_without_reference"),
            r.get("context_recall")))
    print()
    for k in metric_keys:
        v = summary["aggregate"][k]
        if v is not None:
            print(f"  {k:34} {v}")
    print(f"写入 {out_path}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", required=True)
    ap.add_argument("--references", default=str(ROOT / "scripts" / "eval" / "references_v2.json"))
    ap.add_argument("--only", default="kb,drug")
    ap.add_argument("--ids", default="")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--max-contexts", type=int, default=8)
    ap.add_argument("--no-drug-filter", action="store_true",
                    help="drug 切片默认限定 doc_type=drug_instruction（答案证据来自说明书而非通用文档）")
    ap.add_argument("--concurrency", type=int, default=2)
    ap.add_argument("--drug-filter", action="store_true", help="drug 切片检索时限定 doc_type=drug_instruction")
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    warnings.filterwarnings("ignore", category=DeprecationWarning)
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
