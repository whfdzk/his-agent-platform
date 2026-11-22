# src/knowledge/agentic_rag.py
#
# AgenticRAG：检索不再由"一次改写 + 一次召回"决定，而是让检索自己判断三件事：
#   1. 这个问题要不要检索（寒暄/纯常识/与医学无关 → 不检索，省一次 LLM + 两次召回）
#   2. 走哪条通道（文档 / 图谱 / 两者）
#   3. 拿到的证据够不够（不够就带着"缺什么"改写检索式再来一轮，有上限、有停止条件）
#
# 为什么不交给 ReAct Agent 自己循环调用工具：
#   knowledge prompt 里明确要求"只调一个工具"（每次检索都慢，模型容易反复调），
#   而 ReAct 的轮数、是否收敛、为什么停都无法量化。这里把闭环做在固定编排里，
#   轮次/新增证据/停止原因都是可记录、可评测的字段。

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field

from langchain_core.messages import SystemMessage
from loguru import logger

from src.knowledge.deps import KnowledgeDeps

DECIDE_TIMEOUT = 12.0
JUDGE_TIMEOUT = 12.0
MAX_ROUNDS_DEFAULT = 2

DECIDE_PROMPT = """你是医学检索调度员。判断回答下面这个问题需要检索哪些通道。

用户问题：{question}

可用通道：
- doc：临床指南、药品说明书、医院制度、文献原文（有具体数值、剂量、禁忌、阈值）
- graph：疾病/症状/药物/科室/检查之间的实体关系（"什么病用什么药""挂哪个科""什么症状对应什么病"）
- none：寒暄、自我介绍、与医学无关、纯计算

判断规则：
- 问题涉及具体指南条款、剂量、禁忌、指标阈值 → 至少 doc
- 问题涉及某个病用什么药、挂什么科、有哪些症状/并发症 → 至少 graph
- 问题同时需要"指南条款 + 个体情况"（如"糖尿病肾病合并高血压怎么选药"）→ doc 和 graph 都要
- 只有寒暄或与医学无关时才给 none

输出 JSON（只输出 JSON，不要解释）：
{{"need": true, "channels": ["doc", "graph"], "queries": ["细化后的检索式1", "检索式2"], "reason": "一句话理由"}}
queries 给 1-3 条，必须包含问题里的关键实体（病名/药名/指标），不要同义反复。"""

JUDGE_PROMPT = """你在做检索充分性评估。

用户问题：{question}

已检索到的证据：
{evidence}

判断这些证据是否足以回答用户问题。

判够（enough=true）的标准：
- 证据能支撑问题的**核心结论**：该用什么药 / 做什么检查 / 属于什么情况 / 挂什么科
- 关键限定条件也在证据里：禁忌、慎用人群、需要监测的指标
- 即使问题里的组合措辞（如"糖尿病肾病合并高血压"）在指南中没有逐字对应的段落，只要指南分别对该病的治疗原则有明确表述，也算足够
- 证据里已有的具体数值（剂量、阈值）不要求全部齐备，缺个别细节不影响判够——回答时可只写有的部分

判不够（enough=false）只限两种情况：
- 证据与问题基本无关（问的是 A，检索到的全是 B）
- 问题的核心结论（该做什么）在证据里完全没有依据

不足时才需要给 missing 和 followup_queries（1-2 条换角度的检索式）和该走哪条通道。

输出 JSON（只输出 JSON）：
{{"enough": true, "missing": "", "followup_queries": [], "channels": []}}"""

ANSWER_PROMPT = """你是天宫医疗的医学知识助手，请根据下面检索到的证据回答用户问题。

用户角色：{role}
用户问题：{question}

检索证据（文档片段 + 图谱关系）：
{context}

要求：
1. 只根据上述证据回答；证据没覆盖的部分明确说"知识库暂未覆盖"，不要编造指南条款、剂量或文献
2. 引用文档时内联标注【来源：文档名, 第X页】；引用图谱关系时标注【来源：知识图谱】
3. 按用户角色调整深度：医生/药师用专业术语，患者用通俗表达
4. 结尾给一句"如有疑虑请咨询医生"

直接输出回答。"""


@dataclass
class RagRound:
    round: int
    queries: list[str]
    channels: list[str]
    doc_new: int
    graph_new: int
    enough: bool
    missing: str = ""


@dataclass
class AgenticResult:
    answer: str
    rounds: list[RagRound] = field(default_factory=list)
    stop_reason: str = ""
    doc_hits: list[dict] = field(default_factory=list)
    graph_records: list[dict] = field(default_factory=list)
    need_retrieval: bool = True
    degraded: str = ""

    def header(self) -> str:
        """给工具调用方看的自述行：轮次 / 通道 / 证据数 / 为什么停。"""
        ch = "+".join(sorted({c for r in self.rounds for c in r.channels})) or "none"
        return (
            f"【AgenticRAG｜轮次 {len(self.rounds)}｜通道 {ch}｜"
            f"证据 doc{len(self.doc_hits)}/graph{len(self.graph_records)}｜停止：{self.stop_reason}】"
        )


async def _ask_json(llm, prompt: str, timeout: float) -> dict | None:
    """让 LLM 输出 JSON；超时/解析失败返回 None，由调用方走降级路径。"""
    try:
        resp = await asyncio.wait_for(llm.ainvoke([SystemMessage(content=prompt)]), timeout=timeout)
    except Exception as e:
        logger.info(f"AgenticRAG 模型调用失败（{type(e).__name__}），走降级")
        return None
    content = resp.content if isinstance(resp.content, str) else str(resp.content)
    content = content.strip()
    if "```" in content:
        content = content.split("```")[1].lstrip("json").strip()
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        logger.info(f"AgenticRAG 返回非 JSON，走降级：{content[:80]}")
        return None


def _doc_key(hit: dict) -> tuple:
    return (hit.get("doc_name"), hit.get("chunk_index"))


def _graph_key(rec: dict) -> str:
    return json.dumps(rec, ensure_ascii=False, sort_keys=True, default=str)[:200]


def _available_channels(deps: KnowledgeDeps, wanted: list[str]) -> list[str]:
    ok: list[str] = []
    for ch in wanted:
        if ch == "doc" and deps.embedding_model is not None and deps.milvus_client is not None:
            ok.append("doc")
        elif ch == "graph" and deps.neo4j_driver is not None:
            ok.append("graph")
    return ok


async def _retrieve(
    question: str, deps: KnowledgeDeps, channels: list[str]
) -> tuple[list[dict], list[dict]]:
    """跑一轮检索，返回 (doc hits, graph records)。任一路失败都只是空结果，不抛。"""
    docs: list[dict] = []
    graph: list[dict] = []

    if "doc" in channels:
        from src.knowledge.doc_rag import search_docs_raw

        try:
            docs = await search_docs_raw(
                question=question,
                embedding_model=deps.embedding_model,
                milvus_client=deps.milvus_client,
                use_hyde=False,  # 多轮检索里每轮都 HyDE，成本翻倍收益有限
            )
        except Exception as e:
            logger.warning(f"AgenticRAG 文档检索失败: {type(e).__name__}: {e}")

    if "graph" in channels:
        from src.knowledge.graph_rag import search_graph_raw

        try:
            graph = await search_graph_raw(
                question=question, neo4j_driver=deps.neo4j_driver, llm=deps.llm,
            )
        except Exception as e:
            logger.warning(f"AgenticRAG 图谱检索失败: {type(e).__name__}: {e}")

    return docs, graph


def _evidence_brief(doc_hits: list[dict], graph_records: list[dict]) -> str:
    """给充分性判断用的证据摘要。

    截断长度要覆盖一个完整 chunk（512 字）的关键段落：截太短会把"ARB 剂量在下一句"
    当成"没有剂量"，判成证据不足，白跑一轮检索（每轮 ~13s）。
    """
    parts = []
    for i, h in enumerate(doc_hits[:5], 1):
        parts.append(f"[文档{i}]《{h.get('doc_name')}》第{h.get('page_number', '?')}页：{h.get('text', '')[:420]}")
    if graph_records:
        gs = [json.dumps(r, ensure_ascii=False, default=str)[:200] for r in graph_records[:5]]
        parts.append("[图谱]" + " / ".join(gs))
    return "\n".join(parts) if parts else "（无检索结果）"


def _evidence_full(doc_hits: list[dict], graph_records: list[dict]) -> str:
    parts = []
    for i, h in enumerate(doc_hits[:6], 1):
        parts.append(
            f"[文档{i}]《{h.get('doc_name')}》第{h.get('page_number', '?')}页：\n{h.get('text', '')[:700]}"
        )
    if graph_records:
        gs = [json.dumps(r, ensure_ascii=False, default=str)[:260] for r in graph_records[:8]]
        parts.append("[图谱关系]\n" + "\n".join(gs))
    return "\n\n---\n\n".join(parts)


async def run_agentic_rag(
    question: str,
    deps: KnowledgeDeps,
    max_rounds: int = MAX_ROUNDS_DEFAULT,
) -> AgenticResult:
    """自主检索闭环：决策 → 检索 → 充分性判断 → 改写再检索 → 收敛 → 生成回答。"""
    # 1. 检索决策
    decision = await _ask_json(
        deps.llm, DECIDE_PROMPT.format(question=question), DECIDE_TIMEOUT,
    )
    if decision is None:
        # 降级：默认两条通道都试，跟改造前的行为对齐
        decision = {"need": True, "channels": ["doc", "graph"], "queries": [question]}

    if not decision.get("need", True):
        return AgenticResult(
            answer="（本问题无需检索知识库）",
            stop_reason="判定无需检索",
            need_retrieval=False,
        )

    wanted = decision.get("channels") or ["doc"]
    channels = _available_channels(deps, wanted)
    if not channels:
        return AgenticResult(
            answer="知识检索依赖未就绪（向量库/图谱均不可用），请基于通用医学常识回答并说明资料暂缺。",
            stop_reason="通道均不可用",
            degraded="no_channel",
        )

    queries = [q for q in (decision.get("queries") or [question]) if q] or [question]
    query = queries[0]

    doc_hits: list[dict] = []
    graph_records: list[dict] = []
    seen_docs: set = set()
    seen_graph: set = set()
    rounds: list[RagRound] = []
    stop_reason = ""
    enough = False

    # 2~4. 检索 → 判断 → 改写再检索
    for r in range(1, max_rounds + 1):
        new_docs, new_graph = await _retrieve(query, deps, channels)
        doc_new = graph_new = 0
        for h in new_docs:
            k = _doc_key(h)
            if k not in seen_docs:
                seen_docs.add(k)
                doc_hits.append(h)
                doc_new += 1
        for rec in new_graph:
            k = _graph_key(rec)
            if k not in seen_graph:
                seen_graph.add(k)
                graph_records.append(rec)
                graph_new += 1

        logger.info(
            f"AgenticRAG 第 {r} 轮 | query={query!r} channels={channels} "
            f"新增 doc={doc_new} graph={graph_new}"
        )

        # 收敛判断：证据够了就停
        verdict = await _ask_json(
            deps.llm,
            JUDGE_PROMPT.format(
                question=question, evidence=_evidence_brief(doc_hits, graph_records),
            ),
            JUDGE_TIMEOUT,
        )
        if verdict is None:
            enough = bool(doc_hits)
            rounds.append(RagRound(r, [query], channels, doc_new, graph_new, enough, ""))
            stop_reason = "判断失败，按已有证据出结论" if enough else "判断失败且无证据"
            break

        enough = bool(verdict.get("enough"))
        rounds.append(RagRound(
            r, [query], channels, doc_new, graph_new, enough,
            str(verdict.get("missing") or ""),
        ))
        if enough:
            stop_reason = f"证据充分（{r} 轮）"
            break

        # 没有新证据还继续改写，只会烧 token
        if doc_new == 0 and graph_new == 0:
            stop_reason = "本轮无新增证据，停止改写"
            break

        followups = [q for q in (verdict.get("followup_queries") or []) if q]
        if not followups:
            stop_reason = "判断不足但未给出改写式"
            break
        query = followups[0]
        extra_channels = _available_channels(deps, verdict.get("channels") or [])
        channels = list(dict.fromkeys(channels + extra_channels))
        if r == max_rounds:
            stop_reason = f"达到轮次上限 {max_rounds}"

    if not stop_reason:
        stop_reason = f"达到轮次上限 {max_rounds}"

    if not doc_hits and not graph_records:
        return AgenticResult(
            answer="当前知识库与图谱中均未找到与该问题相关的内容。",
            rounds=rounds,
            stop_reason=stop_reason + "（无证据）",
        )

    # 5. 生成回答
    context = _evidence_full(doc_hits, graph_records)
    resp = await deps.llm.ainvoke([SystemMessage(content=ANSWER_PROMPT.format(
        role=deps.role, question=question, context=context,
    ))])
    answer = resp.content if isinstance(resp.content, str) else str(resp.content)

    return AgenticResult(
        answer=answer,
        rounds=rounds,
        stop_reason=stop_reason,
        doc_hits=doc_hits,
        graph_records=graph_records,
    )
