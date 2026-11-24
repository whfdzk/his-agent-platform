"""kb 片 context_recall 的取证脚本：判断"召回低"是检索没给到，还是措辞差异导致 grader 判严。

references_v2.json 里的 reference 是**人工改写过的标准答案**，不是原文段落，
所以不能用"逐字包含"判断召回，只能看两件事：
1. 硬锚点（数值+单位）是否出现在检索结果里；
2. reference 与检索结果的最长公共子串有多长（≥12 字基本说明原文被检索到了）。

输出每条用例的分类结论，用于决定下一步是"调检索"还是"改 label"。

用法：
    python scripts/eval/check_reference_coverage.py --slices kb,drug
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

ANCHOR_RE = re.compile(r"[0-9]+(?:\.[0-9]+)?\s*(?:mmHg|mg|g|克|ml|毫升|次|天|周|小时|分钟|%|度|片|粒|袋)")
MIN_COMMON = 12  # 最长公共子串到这个长度，基本可认定原文被检索到


async def retrieve(question: str, doc_type: str | None) -> list[str]:
    from src.knowledge.deps import build_knowledge_deps  # noqa: PLC0415
    from src.knowledge.doc_rag import format_doc_context, search_docs_raw  # noqa: PLC0415
    from src.knowledge.fusion import CHANNEL_SNIPPET_LIMIT  # noqa: PLC0415

    deps = await build_knowledge_deps()
    hits = await search_docs_raw(
        question=question,
        embedding_model=deps.embedding_model,
        milvus_client=deps.milvus_client,
        doc_type=doc_type,
    )
    seen = format_doc_context(hits)[:CHANNEL_SNIPPET_LIMIT]
    return [c.strip() for c in seen.split("\n\n---\n\n") if c.strip()] if seen else []


def longest_common(a: str, b: str) -> int:
    """最长公共子串长度（滑窗 + 二分加速，纯标准库）。"""
    def has(k: int) -> bool:
        if k <= 0:
            return True
        for i in range(len(a) - k + 1):
            if a[i : i + k] in b:
                return True
        return False

    lo, hi = 0, min(len(a), len(b))
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if has(mid):
            lo = mid
        else:
            hi = mid - 1
    return lo


def diagnose(ref: str, contexts: list[str]) -> dict:
    joined = "\n".join(contexts)
    anchors = [m.group(0).strip() for m in ANCHOR_RE.finditer(ref)]
    hit_anchors = [a for a in anchors if a in joined]
    lcs = max((longest_common(ref[:400], c) for c in contexts), default=0) if contexts else 0
    anchor_ratio = round(len(hit_anchors) / len(anchors), 3) if anchors else None

    if not contexts:
        verdict = "检索空结果"
    elif lcs >= MIN_COMMON and (anchor_ratio is None or anchor_ratio >= 0.6):
        verdict = "检索已给足（原文命中），分数低是 grader 判严"
    elif lcs >= MIN_COMMON:
        verdict = "原文命中但关键数值缺，属部分召回"
    elif anchor_ratio and anchor_ratio >= 0.6:
        verdict = "数值命中但原文未命中，属措辞差异"
    else:
        verdict = "检索没给到，真召回缺失"

    return {
        "contexts": len(contexts),
        "anchors": anchors,
        "anchor_hit": hit_anchors,
        "anchor_ratio": anchor_ratio,
        "longest_common": lcs,
        "verdict": verdict,
    }


async def main_async(args) -> None:
    cases = json.loads((ROOT / "scripts" / "eval" / "cases_v2.json").read_text(encoding="utf-8"))["cases"]
    refs = json.loads((ROOT / "scripts" / "eval" / "references_v2.json").read_text(encoding="utf-8"))
    slices = {s.strip() for s in args.slices.split(",") if s.strip()}

    out = []
    for c in cases:
        if c.get("slice") not in slices:
            continue
        ref = refs.get(c["id"])
        if not ref:
            continue
        doc_type = "drug_instruction" if c["slice"] == "drug" else None
        try:
            contexts = await retrieve(c["question"], doc_type)
        except Exception as e:  # noqa: BLE001
            print(f"  {c['id']:<12} 检索异常：{type(e).__name__}: {e}")
            continue
        d = diagnose(ref, contexts)
        d.update({"id": c["id"], "slice": c["slice"], "question": c["question"], "reference": ref})
        out.append(d)
        print(f"  {c['id']:<12} ctx={d['contexts']:<2} 锚点 {len(d['anchor_hit'])}/{len(d['anchors'])} "
              f"LCS={d['longest_common']:<3} → {d['verdict']}")

    report = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "slices": args.slices,
        "rows": len(out),
        "summary": {},
        "details": out,
    }
    tally: dict[str, int] = {}
    for d in out:
        tally[d["verdict"]] = tally.get(d["verdict"], 0) + 1
    report["summary"] = tally

    path = ROOT / "doc" / "eval-reports" / "reference-coverage.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n汇总：{tally}\n写入 {path}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slices", default="kb,drug")
    args = ap.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
