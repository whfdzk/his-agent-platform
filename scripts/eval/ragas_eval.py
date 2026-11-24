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
import re
import sys
import warnings
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

# 送评前要剔掉的"非事实句"：这些句子本来就不来自检索，留着只会把 faithfulness 分母撑大。
# 注意"建议"本身不能当丢弃词——"建议挂心内科门诊"是结论，必须留。
_BOILERPLATE_PATTERNS = [
    re.compile(r"(尽快|立即|马上).{0,8}(就医|就诊|急诊|拨打\s*120|前往)"),
    re.compile(r"急诊科|拨打\s*120|呼叫\s*120|不要拖"),
    re.compile(r"需要我帮您|帮您挂号|告诉我(日期|时间)|复诊|随访"),
    re.compile(r"携带.{0,8}报告"),
    re.compile(r"请问|能不能|可以请您|有的话|麻烦告诉我|想(进一步)?了解您"),
    re.compile(r"匹配度|智能?问诊助手|基于(现有|目前)的信息|暂时无法|信息(有限|不足)"),
    re.compile(r"建议(今天|明天|尽快)|仅供参考|不能替代|以医生为准"),
]


def _split_sentences(text: str) -> list[str]:
    """按段落和句末标点切句，保留句子本身（不保留分隔符）。"""
    parts: list[str] = []
    for line in text.splitlines():
        line = line.strip().lstrip("*#>- ").strip()
        if not line:
            continue
        for seg in re.split(r"(?<=[。！？；!?;])", line):
            seg = seg.strip()
            if seg:
                parts.append(seg)
    return parts


def _facts_only(answer: str) -> tuple[str, dict]:
    """只留事实性陈述，剔掉安全话术/追问/元话术/免责声明。

    返回 (送评文本, 统计)。统计里 kept/total/ratio 必须写进报告——
    过滤比过低说明答案里几乎没有事实句，这种样本的分数不能直接比。
    """
    sentences = _split_sentences(answer or "")
    total = len(sentences)
    kept = [s for s in sentences if not any(p.search(s) for p in _BOILERPLATE_PATTERNS)]
    ratio = round(len(kept) / total, 3) if total else 0.0
    if not kept:
        # 全被过滤掉说明这条答案里压根没有事实句，退回原文并在报告里标出，避免"过滤成空"抬高分数
        return answer, {"kept": 0, "total": total, "ratio": 0.0, "no_facts": True}
    return "".join(kept), {"kept": len(kept), "total": total, "ratio": ratio, "no_facts": False}


def _drug_core_name(name: str) -> str:
    """把"布洛芬缓释胶囊"剥成"布洛芬"，用于在问题里认出药名。"""
    core = re.sub(r"(肠溶|缓释|控释|分散|咀嚼|泡腾)?(胶囊|片|颗粒|口服液|注射液|软膏|乳膏|滴眼液|喷雾剂|糖浆|栓)", "", name or "")
    return core.strip()


async def _pg_drug_contexts(question: str) -> list[str]:
    """drug 片补 PG 说明书正文。

    真实回答里的适应证/用法用量/禁忌来自 PG 的 medication_tools（drugs + drug_details），
    而 doc_rag 检索只覆盖 Milvus 的 drug_instruction chunk。不把说明书拼进来，
    ragas 就会拿"另一份材料"去判忠实度和召回，分数天然低且不可比。
    """
    import asyncpg  # noqa: PLC0415

    from src.core.config import get_settings  # noqa: PLC0415

    s = get_settings()
    # DATABASE_URL 是 SQLAlchemy 口径（postgresql+asyncpg://），asyncpg.connect 要纯 postgresql://
    dsn = s.DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")
    try:
        conn = await asyncpg.connect(dsn, timeout=10)
    except Exception as e:  # noqa: BLE001 —— 连不上就返回空，不让评测整体失败
        print(f"  [PG] 连接失败，drug 片不补说明书：{type(e).__name__}: {e}")
        return []
    try:
        rows = await conn.fetch(
            "SELECT d.name, d.alias, dd.full_instruction "
            "FROM drugs d JOIN drug_details dd ON dd.drug_id = d.id "
            "WHERE dd.full_instruction IS NOT NULL AND length(dd.full_instruction) > 20"
        )
    except Exception as e:  # noqa: BLE001
        print(f"  [PG] 查说明书失败：{type(e).__name__}: {e}")
        return []
    finally:
        await conn.close()

    hits: list[str] = []
    for r in rows:
        name = r["name"] or ""
        core = _drug_core_name(name)
        if len(core) >= 2 and (core in question or name in question):
            text = (r["full_instruction"] or "").strip()
            if text:
                hits.append(text[:2200])  # 与 CHANNEL_SNIPPET_LIMIT 对齐，模型看到的就这么多
    return hits


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
    fact_stats: dict[str, dict] = {}
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
        if r.get("slice") == "drug":
            # 说明书正文在 PG 不在 Milvus：不补进来，忠实度和召回评的都是另一份材料
            pg_ctx = await _pg_drug_contexts(question)
            if pg_ctx:
                print(f"  {r['id']:<10} 追加 PG 说明书 {len(pg_ctx)} 段", flush=True)
                contexts = pg_ctx + contexts
        if not contexts:
            print(f"  {r['id']:<10} 检索空结果，跳过", flush=True)
            continue
        resp_eval, fact_stat = _facts_only(r["answer"])
        fact_stats[r["id"]] = fact_stat
        rows.append(
            {
                "id": r["id"],
                "slice": r.get("slice"),
                "user_input": question,
                "response": resp_eval,
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
        # ragas 0.4.4 里 collections 版要走 Instructor LLM，跟这里的 LangchainLLMWrapper
        # 不兼容；旧的无参考版只剩私有名导出，按私有名取。
        from ragas.metrics import (  # noqa: PLC0415
            _LLMContextPrecisionWithoutReference as ContextPrecisionWithoutReference,
        )

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
        fs = fact_stats.get(x["id"]) or {}
        row["fact_ratio"] = fs.get("ratio")
        row["facts_kept"] = fs.get("kept")
        row["facts_total"] = fs.get("total")
        row["no_facts"] = fs.get("no_facts", False)
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
    # 先存 Langfuse 再落盘，这样报告里能带上 trace_id 方便回溯
    summary["langfuse_trace_id"] = archive_to_langfuse(summary)
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


def archive_to_langfuse(summary: dict) -> str | None:
    """把四指标汇总推到自托管 Langfuse：一条 trace + 每个指标一条 score。

    直接走公开 ingestion 接口（trace-create / score-create），避免 SDK 大版本差异。
    存档失败只打印原因，不影响评估本身。
    """
    env = read_env()
    if (env.get("LANGFUSE_ENABLED") or "").lower() not in ("1", "true", "yes"):
        print("Langfuse：未启用（LANGFUSE_ENABLED 不为真），跳过存档")
        return None
    pk = env.get("LANGFUSE_PUBLIC_KEY")
    sk = env.get("LANGFUSE_SECRET_KEY")
    base = (env.get("LANGFUSE_BASE_URL") or "").rstrip("/")
    if not (pk and sk and base):
        print("Langfuse：缺 PUBLIC_KEY / SECRET_KEY / BASE_URL，跳过存档")
        return None
    try:
        import uuid  # noqa: PLC0415

        import httpx  # noqa: PLC0415
    except Exception as e:  # pragma: no cover
        print(f"Langfuse：httpx 不可用，跳过存档（{type(e).__name__}）")
        return None

    now_iso = datetime.now().astimezone().isoformat(timespec="seconds")
    trace_id = str(uuid.uuid4())
    scores = summary.get("aggregate") or {}
    slice_tags = ",".join(sorted((summary.get("by_slice") or {}).keys())) or "all"
    name = f"ragas-4metrics-{summary.get('ragas_slices', 'kb,drug')}-{summary.get('rows', 0)}"

    batch: list[dict] = [
        {
            "id": str(uuid.uuid4()),
            "type": "trace-create",
            "timestamp": now_iso,
            "body": {
                "id": trace_id,
                "name": name,
                "timestamp": now_iso,
                "environment": env.get("LANGFUSE_ENVIRONMENT") or "dev",
                "input": {
                    "report": summary.get("report"),
                    "slices": summary.get("ragas_slices"),
                    "rows": summary.get("rows"),
                    "with_reference": summary.get("with_reference"),
                },
                "output": {
                    "aggregate": scores,
                    "by_slice": summary.get("by_slice"),
                },
                "metadata": {
                    "framework": summary.get("framework"),
                    "slice_tags": slice_tags,
                    "details": summary.get("details"),
                },
                "tags": ["ragas", "eval", *[s for s in (summary.get("ragas_slices") or "").split(",") if s]],
            },
        }
    ]
    for k, v in scores.items():
        if v is None:
            continue
        score_id = str(uuid.uuid4())
        batch.append(
            {
                "id": score_id,
                "type": "score-create",
                "timestamp": now_iso,
                "body": {
                    "id": score_id,
                    "traceId": trace_id,
                    "name": k,
                    "value": float(v),
                    "dataType": "NUMERIC",
                    "environment": env.get("LANGFUSE_ENVIRONMENT") or "dev",
                    "comment": f"{summary.get('rows', 0)} 条样本，切片 {summary.get('ragas_slices')}",
                },
            }
        )

    try:
        with httpx.Client(timeout=20) as client:
            resp = client.post(
                f"{base}/api/public/ingestion",
                auth=(pk, sk),
                json={"batch": batch},
            )
        if resp.status_code in (200, 201, 207):
            print(f"Langfuse：已存档 trace={trace_id}（{len(batch) - 1} 个指标）")
            return trace_id
        print(f"Langfuse：存档返回 {resp.status_code} {resp.text[:200]}")
    except Exception as e:  # pragma: no cover
        print(f"Langfuse：存档失败（{type(e).__name__}: {e}）")
    return None


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
