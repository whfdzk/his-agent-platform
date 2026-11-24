"""校准实验：把 contexts 换成答案自身（自证），看 faithfulness 能不能到 1.0。

如果连"用自己的答案当证据"都判不到高分，那就不是答案或检索的问题，
而是 ragas 的 judge 在中文长文本上系统性偏低（默认 prompt 是英文的）。
"""
import asyncio
import json
import sys
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from langchain_openai import ChatOpenAI  # noqa: E402
from ragas import EvaluationDataset, evaluate  # noqa: E402
from ragas.llms import LangchainLLMWrapper  # noqa: E402
from ragas.metrics import Faithfulness  # noqa: E402
from ragas.run_config import RunConfig  # noqa: E402

from scripts.eval.ragas_eval import read_env  # noqa: E402


async def main() -> None:
    env = read_env()
    chat = ChatOpenAI(
        base_url=env.get("BASE_URL_CHAT"),
        api_key=env.get("DASHSCOPE_API_KEY"),
        model=env.get("CHAT_MODEL", "qwen3.8-flash"),
        temperature=0,
        timeout=90,
        extra_body={"enable_thinking": False},
    )
    llm = LangchainLLMWrapper(chat)

    rep = json.loads((ROOT / "doc" / "eval-reports" / "pf-kb-drug-ragas10.json").read_text(encoding="utf-8"))
    ids = ["kb-01", "kb-04", "drug2-布洛芬缓释胶囊-1"]
    rows = []
    for r in rep["results"]:
        if r["id"] not in ids:
            continue
        ans = r.get("answer") or ""
        rows.append({
            "user_input": r.get("question") or "",
            "response": ans,
            # 自证：唯一 context 就是答案本身，正常情况下 faithfulness 应≈1.0
            "retrieved_contexts": [ans],
        })
    print(f"校准样本 {len(rows)} 条（contexts = 答案自身，faithfulness 理应≈1.0）")

    res = evaluate(
        EvaluationDataset.from_list(rows),
        metrics=[Faithfulness(llm=llm)],
        llm=llm,
        run_config=RunConfig(max_workers=1, timeout=180),
        raise_exceptions=False,
        show_progress=False,
    )
    df = res.to_pandas()
    for _, row in df.iterrows():
        print(f"  {row['user_input'][:26]:<28} faithfulness={row['faithfulness']}")


if __name__ == "__main__":
    warnings.filterwarnings("ignore")
    asyncio.run(main())
