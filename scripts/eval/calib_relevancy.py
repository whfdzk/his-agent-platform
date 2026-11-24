"""对照实验：同一问题，完整答案 vs 极简答案，看 AnswerRelevancy 是否对"信息量大"有惩罚。"""
import asyncio
import json
import sys
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from langchain_openai import ChatOpenAI  # noqa: E402
from ragas import EvaluationDataset, evaluate  # noqa: E402
from ragas.embeddings import LangchainEmbeddingsWrapper  # noqa: E402
from ragas.llms import LangchainLLMWrapper  # noqa: E402
from ragas.metrics import AnswerRelevancy  # noqa: E402
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
    from src.infra.embedding import get_embeddings  # noqa: PLC0415

    emb = LangchainEmbeddingsWrapper(get_embeddings())

    rep = json.loads((ROOT / "doc" / "eval-reports" / "pf-kb-drug-ragas10.json").read_text(encoding="utf-8"))
    full = next(r for r in rep["results"] if r["id"] == "kb-01")
    q = full["question"]

    rows = [
        {"user_input": q, "response": full["answer"], "retrieved_contexts": [""]},
        {"user_input": q, "response": "诊室血压非同日三次≥140/90mmHg 即可诊断高血压，家庭自测≥135/85mmHg。", "retrieved_contexts": [""]},
    ]
    res = evaluate(
        EvaluationDataset.from_list(rows),
        metrics=[AnswerRelevancy(llm=llm, embeddings=emb)],
        llm=llm,
        embeddings=emb,
        run_config=RunConfig(max_workers=1, timeout=180),
        raise_exceptions=False,
        show_progress=False,
    )
    df = res.to_pandas()
    for _, row in df.iterrows():
        print(f"  答案长度 {len(row['response']):>4} 字 → answer_relevancy={row['answer_relevancy']}")


if __name__ == "__main__":
    warnings.filterwarnings("ignore")
    asyncio.run(main())
