# src/mcp/server.py
#
# MCP Server：把 his-agent-platform 的医疗工具包暴露成 MCP 协议。
# 基于 FastMCP，可被 Claude Desktop、Cursor 等 MCP Client 直接连接。
#
# 数据来源全部为本平台的真实存储，不含演示数据：
#   - 疾病/症状/科室关系：Neo4j 医疗图谱
#   - 药品说明书、科室目录、医院目录：PostgreSQL
#   - 医学知识问答：Milvus 知识库（向量 + BM25 混合检索 + 精排）
#
# 用法：
#   1. 依赖：fastmcp（已安装 3.2.4）
#   2. stdio 启动（Claude Desktop / Cursor 用这个）：python -m src.mcp.server
#   3. HTTP 启动（便于 curl/远程接入）：python -m src.mcp.server --http --port 8765
#   4. Claude Desktop 配置：
#      {
#        "mcpServers": {
#          "his-agent": {
#            "command": "D:\\conda_envs\\langchain1.2\\python.exe",
#            "args": ["-m", "src.mcp.server"],
#            "cwd": "<项目根>"
#          }
#        }
#      }

from __future__ import annotations

import argparse
import sys

from fastmcp import FastMCP
from loguru import logger
from sqlalchemy import text

from src.infra.database import AsyncSessionLocal
from src.infra.neo4j_client import get_neo4j_driver

mcp = FastMCP(
    "his-agent-platform",
    instructions=(
        "医疗 AI Agent 中台的 MCP 工具集：疾病图谱检索、药品说明书、医学知识库问答、"
        "科室与医院目录。数据全部来自平台真实数据库（Neo4j 图谱 + PostgreSQL + Milvus 知识库）。"
        "注意：本工具集提供的是辅助参考信息，不构成诊断或处方。"
    ),
)


# ══════════════════ 医疗知识工具 ══════════════════


@mcp.tool()
async def search_disease_by_symptom(symptom: str, limit: int = 8) -> dict:
    """按症状检索可能相关的疾病（Neo4j 医疗图谱，真实数据）。

    Args:
        symptom: 症状关键词，如「头痛」「发热」「咳嗽」
        limit: 返回疾病条数上限，默认 8
    """
    kw = (symptom or "").strip()
    if not kw:
        return {"error": "symptom 不能为空", "diseases": []}
    limit = max(1, min(int(limit), 20))

    cypher = """
    MATCH (d:Disease)-[:HAS_SYMPTOM]->(st:Symptom)
    WHERE st.name CONTAINS $kw OR d.name CONTAINS $kw
    OPTIONAL MATCH (d)-[:BELONGS_TO]->(dept:Department)
    RETURN d.name AS name,
           d.description AS description,
           d.cure_way AS cure_way,
           d.cured_prob AS cured_prob,
           d.cost_money AS cost_money,
           collect(DISTINCT st.name)[0..6] AS symptoms,
           collect(DISTINCT dept.name)[0..3] AS departments
    LIMIT $limit
    """
    driver = get_neo4j_driver()
    try:
        async with driver.session() as s:
            result = await s.run(cypher, kw=kw, limit=limit)
            rows = [dict(r) async for r in result]
    except Exception as e:  # 图谱不可用不应让 MCP 工具整条挂掉
        logger.warning(f"MCP search_disease_by_symptom 失败: {e}")
        return {"error": f"图谱查询失败: {e}", "diseases": []}

    return {
        "query": kw,
        "count": len(rows),
        "diseases": rows,
        "source": "neo4j: Disease-[:HAS_SYMPTOM]->Symptom",
        "disclaimer": "仅供辅助参考，不能替代医生诊断。",
    }


@mcp.tool()
async def get_drug_info(drug_name: str) -> dict:
    """查询药品说明书信息：适应症、用法用量、不良反应、禁忌、注意事项。

    Args:
        drug_name: 药品名称或别名，如「阿莫西林」「布洛芬」
    """
    kw = (drug_name or "").strip()
    if not kw:
        return {"error": "drug_name 不能为空", "found": False}

    sql = text(
        """
        SELECT d.id, d.name, d.alias, d.category, d.is_otc, d.price,
               dd.indication, dd.usage_dosage, dd.adverse_reaction,
               dd.contraindication, dd.precaution, dd.interaction, dd.full_instruction
        FROM drugs d
        LEFT JOIN drug_details dd ON dd.drug_id = d.id
        WHERE d.name ILIKE :kw OR d.alias ILIKE :kw
        ORDER BY d.id
        LIMIT 3
        """
    )
    try:
        async with AsyncSessionLocal() as s:
            rows = (await s.execute(sql, {"kw": f"%{kw}%"})).mappings().all()
    except Exception as e:
        logger.warning(f"MCP get_drug_info 失败: {e}")
        return {"error": f"药品库查询失败: {e}", "found": False}

    if not rows:
        return {
            "query": kw,
            "found": False,
            "message": f"药品库中没有「{kw}」，请换用通用名或商品名重试",
        }

    best = rows[0]
    return {
        "query": kw,
        "found": True,
        "drug": {
            "name": best["name"],
            "alias": best["alias"],
            "category": best["category"],
            "is_otc": best["is_otc"],
            "price": best["price"],
            "indication": best["indication"],
            "usage_dosage": best["usage_dosage"],
            "adverse_reaction": best["adverse_reaction"],
            "contraindication": best["contraindication"],
            "precaution": best["precaution"],
            "interaction": best["interaction"],
        },
        "matched_count": len(rows),
        "source": "postgres: drugs + drug_details",
        "disclaimer": "用药请遵医嘱，本信息不构成处方建议。",
    }


@mcp.tool()
async def search_medical_question(question: str, top_k: int = 5) -> dict:
    """医学知识库检索（Milvus 向量 + BM25 混合召回 + Rerank 精排）。

    返回的是知识库原文片段（带出处与分数），不是模型生成的答案。

    Args:
        question: 医学问题，如「高血压的诊断标准是什么」
        top_k: 返回片段数，默认 5
    """
    q = (question or "").strip()
    if not q:
        return {"error": "question 不能为空", "evidence": []}
    top_k = max(1, min(int(top_k), 10))

    try:
        from src.infra.embedding import get_embeddings
        from src.infra.milvus_client import get_milvus_client
        from src.knowledge.doc_rag import search_docs_raw

        docs = await search_docs_raw(
            q, get_embeddings(), get_milvus_client(), rerank_top_k=top_k,
        )
    except Exception as e:
        logger.warning(f"MCP search_medical_question 失败: {e}")
        return {"error": f"知识库检索失败: {e}", "evidence": []}

    evidence = [
        {
            "text": d.get("text", ""),
            "doc_name": d.get("doc_name", ""),
            "doc_type": d.get("doc_type", ""),
            "page": d.get("page_number"),
            "chunk_index": d.get("chunk_index"),
            "score": round(float(d.get("score") or 0.0), 4),
            "rerank_score": (
                round(float(d["rerank_score"]), 4) if d.get("rerank_score") is not None else None
            ),
            "retriever": d.get("retriever", ""),
        }
        for d in docs
    ]
    return {
        "question": q,
        "count": len(evidence),
        "evidence": evidence,
        "source": "milvus: knowledge_docs (vector + bm25 + rerank)",
        "note": "知识库为空时 count 为 0，需先在管理端上传文档。",
    }


# ══════════════════ 医院导诊工具 ══════════════════


@mcp.tool()
async def list_departments() -> dict:
    """列出平台维护的全部科室（PostgreSQL，真实数据）。"""
    sql = text("SELECT id, name, description FROM departments ORDER BY id")
    try:
        async with AsyncSessionLocal() as s:
            rows = (await s.execute(sql)).mappings().all()
    except Exception as e:
        logger.warning(f"MCP list_departments 失败: {e}")
        return {"error": f"科室查询失败: {e}", "departments": []}
    return {"count": len(rows), "departments": [dict(r) for r in rows], "source": "postgres: departments"}


@mcp.tool()
async def list_hospitals(city: str | None = None) -> dict:
    """列出平台合作医院，可按城市过滤（PostgreSQL，真实数据）。

    Args:
        city: 城市名，如「北京」；不传则返回全部
    """
    # asyncpg 无法推断「:param IS NULL」的参数类型（IndeterminateDatatypeError），
    # 所以按是否传 city 分成两条 SQL，而不是用一个可空参数兜两种语义。
    cols = (
        "SELECT id, name, level, hospital_type, province, city, address, "
        "phone, emergency_phone, open_hours FROM hospitals "
    )
    if city and city.strip():
        sql = text(cols + "WHERE city ILIKE :city_like ORDER BY id")
        params = {"city_like": f"%{city.strip()}%"}
    else:
        sql = text(cols + "ORDER BY id")
        params = {}
    try:
        async with AsyncSessionLocal() as s:
            rows = (await s.execute(sql, params)).mappings().all()
    except Exception as e:
        logger.warning(f"MCP list_hospitals 失败: {e}")
        return {"error": f"医院查询失败: {e}", "hospitals": []}
    return {"count": len(rows), "hospitals": [dict(r) for r in rows], "source": "postgres: hospitals"}


# ══════════════════ 提示词资源（读库，含激活版本） ══════════════════


async def _load_agent_system_prompt(code: str) -> tuple[str, str]:
    """返回 (提示词正文, 来源说明)。库里有激活版本就用库里的，否则用代码内置默认。"""
    sql = text("SELECT config FROM agents WHERE code = :code LIMIT 1")
    prompt_sql = text(
        """
        SELECT pv.content, p.active_version
        FROM prompts p JOIN prompt_versions pv
          ON pv.prompt_id = p.id AND pv.version = p.active_version
        WHERE p.id = :pid AND p.enabled = TRUE
        """
    )
    try:
        async with AsyncSessionLocal() as s:
            cfg = (await s.execute(sql, {"code": code})).scalar()
            if isinstance(cfg, dict):
                pid = cfg.get("prompt_id")
                inline = (cfg.get("system_prompt") or "").strip()
            else:
                pid, inline = None, ""
            if isinstance(pid, int) and pid > 0:
                row = (await s.execute(prompt_sql, {"pid": pid})).mappings().first()
                if row:
                    return row["content"], f"prompts/{code}#v{row['active_version']}"
            if inline:
                return inline, f"agents/{code}#config.system_prompt"
    except Exception as e:
        logger.warning(f"MCP 读取提示词失败（{code}）: {e}")

    return _FALLBACK_PROMPTS.get(code, ""), "builtin:fallback"


_FALLBACK_PROMPTS = {
    "supervisor": (
        "你是医疗问诊 Supervisor，负责把患者问题路由给合适的子 Agent：\n"
        "- inquiry_agent：症状采集与初步分诊\n"
        "- drug_agent：用药咨询\n"
        "- report_agent：检查报告解读\n"
        "- knowledge_agent：医学知识问答\n"
        "涉及危重症状时优先提示立即就医。你不能给出确诊结论。"
    ),
    "inquiry": (
        "你是专业问诊医生，需采集主诉、现病史、既往史、过敏史、用药史等关键信息，"
        "再给出可能的就诊方向。注意：你不能给出确诊，只能建议就诊科室并提示风险。"
    ),
}


@mcp.resource("prompts://supervisor/system")
async def get_supervisor_prompt() -> str:
    """Supervisor Agent 的系统提示词（优先取管理端配置的激活版本）。"""
    content, origin = await _load_agent_system_prompt("supervisor")
    return f"<!-- source: {origin} -->\n{content}"


@mcp.resource("prompts://inquiry/system")
async def get_inquiry_prompt() -> str:
    """问诊 Agent 的系统提示词（优先取管理端配置的激活版本）。"""
    content, origin = await _load_agent_system_prompt("inquiry")
    return f"<!-- source: {origin} -->\n{content}"


# ══════════════════ 启动入口 ══════════════════


def main() -> None:
    parser = argparse.ArgumentParser(description="his-agent-platform MCP Server")
    parser.add_argument("--http", action="store_true", help="以 HTTP 方式启动（默认 stdio）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    if args.http:
        logger.info(f"启动 MCP Server（HTTP）: http://{args.host}:{args.port}/mcp")
        mcp.run(transport="http", host=args.host, port=args.port)
    else:
        # stdio 模式：MCP Client（Claude Desktop / Cursor）拉起本进程通信
        logger.info("启动 MCP Server（stdio）")
        mcp.run()


if __name__ == "__main__":
    sys.exit(main())
