# src/knowledge/tools.py
#
# 知识链路工具集：文档 RAG / 图谱 RAG / 多通道融合 / 处方审核 / NL2SQL。
#
# 依赖通过 contextvar（src.knowledge.deps）在调用时解析：
# Agent 保持单例，工具在真正被调用时才拿当前请求的 llm/向量库/图谱/DB 会话。
# 任一基础设施不可用时返回可读提示而不是抛异常，避免整轮对话被判死。

from __future__ import annotations

import asyncio
import json

from langchain_core.tools import tool
from loguru import logger

from src.core.config import get_settings
from src.knowledge.audit import QueryAuditLog, Timer
from src.knowledge.deps import KnowledgeDeps, current_knowledge_deps

__all__ = ["KnowledgeDeps", "build_knowledge_tools"]

# Query 改写的预算：这一步只是"锦上添花"，模型慢的时候不能拖住整轮问答
REWRITE_TIMEOUT = 10.0
# 工具回给模型的最大原文片段长度：够写出有依据的答案，又不至于把上下文顶爆
DOC_CONTEXT_MAX_CHARS = 3000

settings = get_settings()

# 知识来源类型 → 中文标签（来源清单展示用，上传端也用同一套取值）
KNOWLEDGE_TYPE_LABELS = {
    "guideline": "临床指南",
    "drug_instruction": "药品说明书",
    "sop": "医院制度/SOP",
    "literature": "医学文献",
    "disease": "疾病科普",
    "lab_indicator": "检验指标",
    "department": "就医科室",
    "other": "其它",
}


class _DepsUnavailable(RuntimeError):
    """当前请求没有绑定知识依赖（例如被单测直接调用）。"""


def _require_deps() -> KnowledgeDeps:
    deps = current_knowledge_deps()
    if deps is None:
        raise _DepsUnavailable("知识检索依赖未初始化")
    return deps


async def _rewrite(question: str, deps: KnowledgeDeps) -> str:
    """Query 改写：口语→医学术语。超时或失败时退回原问题。"""
    from src.knowledge.query_rewriter import rewrite_query

    try:
        result = await asyncio.wait_for(
            rewrite_query(question, deps.util_llm, deps.role), timeout=REWRITE_TIMEOUT
        )
        return result["queries"][0] if result.get("queries") else question
    except Exception as e:
        logger.info(f"Query 改写跳过（{type(e).__name__}），直接检索原问题")
        return question


def build_knowledge_tools(deps: KnowledgeDeps | None = None) -> list:
    """构建知识 Agent 工具集。

    deps 显式传入时固定使用该依赖（便于测试/脚本）；
    默认 None 时每次调用从 contextvar 取当前请求的依赖。
    """

    def _deps() -> KnowledgeDeps:
        return deps or _require_deps()

    @tool
    async def search_knowledge_docs(question: str, doc_type: str = "") -> str:
        """从知识文档库中检索原文片段（指南/说明书/SOP/文献），由你据片段作答。
        适用：查询临床指南、药品说明书、医院制度SOP、医学文献等文档内容。
        question: 用户的问题
        doc_type: 可选，文档类型过滤（guideline/drug_instruction/sop/literature）。
            谨慎使用：只有确定答案一定在该类文档里时才传，否则留空查全库。
            问"某类药的禁忌/不良反应/能不能用"时通常不要过滤——
            禁忌和适应证多写在指南里，只查说明书会漏掉关键条款。"""
        from src.knowledge.cache import cached, make_key
        from src.knowledge.doc_rag import format_doc_context, search_docs_raw

        try:
            d = _deps()
            if d.milvus_client is None or d.embedding_model is None:
                return "知识文档库暂时不可用（向量库未就绪），请基于通用医学常识回答，并说明资料暂缺。"

            async def _compute() -> str:
                # 只回检索到的原文片段，不在工具里再"生成一篇回答"。
                # 实测：工具侧生成 2857 token 用了 35s，agent 拿到后再压缩成自己的回答
                # 又是 20s，同一份内容写两遍占了整轮 70s 的大头。
                # 改写（_rewrite）也一并去掉：检索片段够用时它只是多一次模型调用。
                hits = await search_docs_raw(
                    question=question, embedding_model=d.embedding_model,
                    milvus_client=d.milvus_client,
                    doc_type=doc_type or None,
                    # HyDE 会多一次 LLM 调用，患者问答场景性价比不高，默认不用
                    use_hyde=False,
                    # 缓存键用工具入参的原始问题：改写措辞每次都不一样，用改写结果做键命中不了
                    cache_question=question,
                )
                return format_doc_context(hits)[:DOC_CONTEXT_MAX_CHARS]

            with Timer() as t:
                result = await cached(
                    make_key("tool_docs", question.strip(), doc_type or "", d.role),
                    _compute,
                    label=f"tool_docs:{question[:12]}",
                )
            QueryAuditLog.log(
                d.user_id, d.role, question, "doc_rag",
                ["doc_rag"], result[:80], t.elapsed_ms,
            )
            return result or "知识文档库中未检索到与该问题相关的片段。"
        except _DepsUnavailable:
            return "知识检索依赖未初始化，请基于通用医学常识回答，并说明资料暂缺。"
        except Exception as e:
            logger.warning(f"文档检索失败: {type(e).__name__}: {e}")
            return "知识文档检索暂时失败，请基于通用医学常识回答，并提示用户稍后再查。"

    @tool
    async def search_knowledge_graph(question: str) -> str:
        """从医学知识图谱中检索实体关系并生成回答。
        适用：查询疾病与症状/药物/科室/检查/食物之间的关系，多跳推理。
        question: 用户的问题"""
        from src.knowledge.cache import cached, make_key
        from src.knowledge.graph_rag import search_graph_raw

        try:
            d = _deps()
            if d.neo4j_driver is None:
                return "知识图谱暂时不可用，请基于通用医学常识回答，并说明资料暂缺。"

            async def _compute() -> str:
                # 只回原始关系记录，不再在工具里"整合成一篇回答"：调用方 agent 自己就会写，
                # 工具侧再写一遍会多一次 600 token / 9s 的生成，且两段结论口径容易打架。
                records = await search_graph_raw(
                    question=question, neo4j_driver=d.neo4j_driver, llm=d.util_llm or d.llm,
                    cache_question=question,
                )
                if not records:
                    return ""
                return json.dumps(records, ensure_ascii=False)[:1800]

            with Timer() as t:
                result = await cached(
                    make_key("tool_graph", question.strip(), d.role),
                    _compute,
                    label=f"tool_graph:{question[:12]}",
                )
            QueryAuditLog.log(
                d.user_id, d.role, question, "graph_rag",
                ["graph_rag"], result[:80], t.elapsed_ms,
            )
            return result
        except _DepsUnavailable:
            return "知识检索依赖未初始化，请基于通用医学常识回答，并说明资料暂缺。"
        except Exception as e:
            logger.warning(f"图谱检索失败: {type(e).__name__}: {e}")
            return "知识图谱检索暂时失败，请基于通用医学常识回答，并提示用户稍后再查。"

    @tool
    async def search_knowledge_sql(question: str) -> str:
        """从运营数据库中查询统计数据并生成回答。
        适用：查询问诊量、药品库存、科室排名、收入统计等结构化数据（仅限内部运营/医生角色）。
        question: 用户的问题"""
        try:
            d = _deps()
        except _DepsUnavailable:
            return "运营数据查询依赖未初始化。"
        if not d.is_staff:
            # 患者端不允许触达运营库：即使模型误调也只能拿到这句拒绝
            return "运营数据查询仅对内部运营人员开放，当前账号无权访问。"
        if d.db_session is None:
            return "数据库连接不可用，无法执行查询。"

        from src.knowledge.nl2sql import search_sql

        try:
            with Timer() as t:
                result = await search_sql(question=question, llm=d.util_llm, db=d.db_session)
            QueryAuditLog.log(
                d.user_id, d.role, question, "nl2sql",
                ["nl2sql"], result[:80], t.elapsed_ms,
            )
            return result
        except Exception as e:
            logger.warning(f"NL2SQL 查询失败: {type(e).__name__}: {e}")
            return "运营数据查询失败，请稍后重试。"

    @tool
    async def search_knowledge_multi(question: str) -> str:
        """多通道检索：一次并行查文档库和知识图谱，返回两路证据原文，由你据证据作答。
        适用：复杂问题，需要同时参考指南文档和实体关系才能回答。
        例如："糖尿病合并肾功能不全的降糖方案"需要同时查指南和药物禁忌。
        question: 用户的问题"""
        from src.knowledge.cache import cached, make_key
        from src.knowledge.fusion import multi_channel_search

        try:
            d = _deps()
            if (d.milvus_client is None or d.embedding_model is None) and d.neo4j_driver is None:
                return "知识库暂时不可用，请基于通用医学常识回答，并说明资料暂缺。"

            async def _compute() -> str:
                return await multi_channel_search(
                    question=question, llm=d.llm,
                    embedding_model=d.embedding_model,
                    milvus_client=d.milvus_client,
                    neo4j_driver=d.neo4j_driver,
                    db_session=d.db_session,
                    role=d.role,
                    util_llm=d.util_llm,
                    cache_question=question,
                )

            with Timer() as t:
                result = await cached(
                    make_key("tool_multi", question.strip(), d.role),
                    _compute,
                    label=f"tool_multi:{question[:12]}",
                )
            QueryAuditLog.log(
                d.user_id, d.role, question, "multi",
                ["doc_rag", "graph_rag"], result[:80], t.elapsed_ms,
            )
            return result
        except _DepsUnavailable:
            return "知识检索依赖未初始化，请基于通用医学常识回答，并说明资料暂缺。"
        except Exception as e:
            logger.warning(f"多通道检索失败: {type(e).__name__}: {e}")
            return "多通道检索暂时失败，请基于通用医学常识回答，并提示用户稍后再查。"

    @tool
    async def search_knowledge_agentic(question: str) -> str:
        """自主多轮检索：自行判断要不要检索、走文档还是图谱，证据不足时带着"缺什么"
        改写检索式再查一轮（最多 2 轮，有停止条件），最后给带来源的回答。
        适用：复杂临床问题，需要同时看指南条款与实体关系，一次检索通常答不全。
        例如："糖尿病肾病合并高血压该怎么选降压药"、"ACEI 引起干咳能换成什么药"。
        question: 用户的问题"""
        from src.knowledge.agentic_rag import run_agentic_rag

        try:
            d = _deps()
        except _DepsUnavailable:
            return "知识检索依赖未初始化，请基于通用医学常识回答，并说明资料暂缺。"

        if (d.milvus_client is None or d.embedding_model is None) and d.neo4j_driver is None:
            return "知识库暂时不可用，请基于通用医学常识回答，并说明资料暂缺。"

        try:
            with Timer() as t:
                result = await run_agentic_rag(question, d)
            if not result.need_retrieval:
                # 交给 Agent 用常识回答，不要硬塞一次检索结果
                return result.answer
            QueryAuditLog.log(
                d.user_id, d.role, question, "agentic",
                (sorted({c for r in result.rounds for c in r.channels}) or ["doc_rag"]),
                result.answer[:80], t.elapsed_ms,
            )
            logger.info(
                f"AgenticRAG 完成 | 轮次={len(result.rounds)} "
                f"证据 doc={len(result.doc_hits)} graph={len(result.graph_records)} "
                f"停止={result.stop_reason} 耗时={t.elapsed_ms:.0f}ms"
            )
            return f"{result.header()}\n\n{result.answer}"
        except Exception as e:
            logger.warning(f"AgenticRAG 失败: {type(e).__name__}: {e}")
            return "自主检索暂时失败，请基于通用医学常识回答，并提示用户稍后再查。"

    @tool
    async def review_prescription_tool(question: str) -> str:
        """处方审核：校验处方中的药品剂量、配伍禁忌、过敏冲突、重复用药。
        适用：患者咨询"这两种药能否同服"、药师审核处方安全性、医生确认用药方案是否合理。
        question: 处方/用药信息描述（药品名+剂量+患者信息）"""
        from src.knowledge.prescription_review import review_prescription

        try:
            d = _deps()
            if d.milvus_client is None or d.embedding_model is None or d.neo4j_driver is None:
                return "处方审核所需的知识库暂不可用，请仅给出通用用药提示，并建议咨询药师。"
            with Timer() as t:
                result = await review_prescription(
                    question=question, llm=d.llm,
                    embedding_model=d.embedding_model,
                    milvus_client=d.milvus_client,
                    neo4j_driver=d.neo4j_driver,
                )
            QueryAuditLog.log(
                d.user_id, d.role, question, "prescription",
                ["doc_rag", "graph_rag"], result[:80], t.elapsed_ms,
            )
            return result
        except _DepsUnavailable:
            return "处方审核依赖未初始化，请仅给出通用用药提示，并建议咨询药师。"
        except Exception as e:
            logger.warning(f"处方审核失败: {type(e).__name__}: {e}")
            return "处方审核服务暂时不可用，请提示用户咨询药师或医生。"

    @tool
    async def list_knowledge_sources(doc_type: str = "") -> str:
        """列出知识库的来源与版本：已收录文档清单、各类型数量与来源分类。
        适用：用户问"这些说法出自哪里""知识库有哪些指南/说明书""资料是最新的吗"。
        doc_type: 可选过滤，guideline/drug_instruction/sop/literature/disease/lab_indicator/department"""
        try:
            d = _deps()
        except _DepsUnavailable:
            return "知识检索依赖未初始化，无法列出知识来源。"
        if d.milvus_client is None:
            return "知识库暂时不可用（向量库未就绪），无法列出知识来源。"

        from src.knowledge.doc_ingestion import ensure_knowledge_collection
        from src.knowledge.doc_rag import COLLECTION_NAME
        from src.core.concurrency import to_thread

        try:
            await to_thread(ensure_knowledge_collection, d.milvus_client)
            flt = "chunk_index == 0"
            if doc_type:
                flt += f' && doc_type == "{doc_type}"'
            rows = await to_thread(
                d.milvus_client.query,
                collection_name=COLLECTION_NAME,
                filter=flt,
                output_fields=["doc_id", "doc_name", "doc_type", "category"],
                limit=settings.DOC_LIST_LIMIT,
            )
        except Exception as e:
            logger.warning(f"知识来源列表查询失败: {type(e).__name__}: {e}")
            return "知识来源查询暂时失败，请稍后再查。"

        if not rows:
            return "知识库当前没有收录文档（来源为空）。请先由管理端上传指南/说明书后再查询。"

        by_type: dict[str, list[str]] = {}
        seen: set[str] = set()
        for r in rows:
            if r["doc_id"] in seen:
                continue
            seen.add(r["doc_id"])
            by_type.setdefault(r.get("doc_type") or "other", []).append(r["doc_name"])

        lines = ["【知识库来源清单】"]
        for dtype, names in sorted(by_type.items(), key=lambda kv: -len(kv[1])):
            lines.append(f"\n{KNOWLEDGE_TYPE_LABELS.get(dtype, dtype)}（{len(names)} 份）")
            for n in names[:10]:
                lines.append(f"- {n}")
            if len(names) > 10:
                lines.append(f"- ……另有 {len(names) - 10} 份")
        lines.append(
            "\n版本说明：同名文档重新导入即整体覆盖为最新版，版本以 doc_id（文档名摘要）标识；"
            "文档自身标注版本号时以上传文件为准。"
        )
        return "\n".join(lines)

    return [
        search_knowledge_docs,
        search_knowledge_graph,
        search_knowledge_sql,
        search_knowledge_multi,
        search_knowledge_agentic,
        review_prescription_tool,
        list_knowledge_sources,
    ]
