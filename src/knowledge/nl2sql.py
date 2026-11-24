# src/agents/knowledge/nl2sql.py

from __future__ import annotations
import asyncio
import re
import json
from loguru import logger
from langchain_core.messages import SystemMessage
from langchain_core.language_models import BaseChatModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

from src.core.config import get_settings
from src.core.redaction import redact_text
from src.knowledge.llm_call import ANSWER_TIMEOUT, UTIL_TIMEOUT, call_llm_text
from src.knowledge.prompts import NL2SQL_PROMPT, SQL_QA_PROMPT

settings = get_settings()
MAX_SQL_RETRIES = settings.NL2SQL_MAX_RETRIES
SQL_TIMEOUT_SECONDS = settings.NL2SQL_TIMEOUT_SECONDS

FORBIDDEN_PATTERNS = [
    re.compile(r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|GRANT|REVOKE)\b", re.IGNORECASE),
    # 管理/系统对象：admins 里有 password_hash，系统目录可反查全库结构
    re.compile(r"\b(admins|information_schema|pg_catalog|pg_shadow|pg_user|pg_authid)\b", re.IGNORECASE),
    re.compile(r"\b(pg_sleep|pg_read_file|pg_ls_dir|lo_import|dblink)\b", re.IGNORECASE),
    # 注释后会截断语句，是绕过关键字匹配的常见手法
    re.compile(r"(--|/\*)"),
]

# 表白名单：只有运营分析真正需要的表可查（与 NL2SQL_PROMPT 中的表结构一一对应）
ALLOWED_TABLES = frozenset({
    "departments", "diseases", "symptoms", "drugs",
    "consultations", "disease_symptoms", "disease_drugs",
})
_TABLE_REF_RE = re.compile(r"\b(?:from|join)\s+([a-zA-Z_][\w]*)", re.IGNORECASE)

# 患者隐私字段：只要与 patients 表同时出现即拦截。
# 注意不能写成 "patients 后面跟着敏感列" 的顺序型正则 —— 那样
# `SELECT phone FROM patients` 就能绕过校验，直接泄漏 PHI。
_PATIENTS_TABLE_RE = re.compile(r"\bpatients\b", re.IGNORECASE)
_SENSITIVE_COLUMNS = ("phone", "id_card")
# count(*) 是安全的聚合，不算通配符，先摘掉再判断是否残留 "*"
_AGG_STAR_RE = re.compile(r"\bcount\s*\(\s*\*\s*\)", re.IGNORECASE)
_LIMIT_RE = re.compile(r"\blimit\s+(\d+)", re.IGNORECASE)
MAX_LIMIT = 100


def _validate_sql(sql: str) -> tuple[bool, str]:
    """NL2SQL 结果校验：只允许单条、只读、限表、限量的 SELECT。

    模型输出不可信，这里做的是"纵深防御的第二层"，第一层是只读数据库账号。
    """
    stripped = sql.strip()
    if not stripped:
        return False, "SQL 为空"
    # 多语句：即使是 "SELECT 1; SELECT 2" 也只执行第一条，直接拒绝更安全
    if ";" in stripped.rstrip(";"):
        return False, "只允许单条语句"
    stripped = stripped.rstrip(";").strip()

    if not stripped.upper().startswith("SELECT"):
        return False, "只允许 SELECT 查询"
    for pattern in FORBIDDEN_PATTERNS:
        if pattern.search(stripped):
            return False, "查询包含禁止的操作或对象"

    # 表白名单：FROM/JOIN 后面出现的标识符必须全部在允许范围内
    tables = {t.lower() for t in _TABLE_REF_RE.findall(stripped)}
    if not tables:
        return False, "未识别到查询的表"
    unknown = tables - ALLOWED_TABLES
    if unknown:
        return False, f"不允许查询表：{', '.join(sorted(unknown))}"

    if _PATIENTS_TABLE_RE.search(stripped):
        probe = _AGG_STAR_RE.sub("", stripped).lower()
        if any(col in probe for col in _SENSITIVE_COLUMNS) or "*" in probe:
            return False, "查询包含禁止的患者隐私字段"

    # 强制 LIMIT：缺失则补；超过上限则收紧到上限（不直接拒绝，避免模型因此重试烧 token）
    match = _LIMIT_RE.search(stripped)
    if not match:
        stripped += f" LIMIT {MAX_LIMIT}"
    elif int(match.group(1)) > MAX_LIMIT:
        stripped = _LIMIT_RE.sub(f"LIMIT {MAX_LIMIT}", stripped)
    return True, stripped


async def _generate_sql(
    question: str, llm: BaseChatModel, error_hint: str = "",
) -> str:
    extra = ""
    if error_hint:
        extra = f"\n\n上一次生成的 SQL 执行报错：{error_hint}\n请修正后重新生成。"
    prompt = NL2SQL_PROMPT.format(question=question) + extra
    # 超时返回空串：下游 _validate_sql 会判为非法并直接终止，不会拿半截 SQL 去执行
    sql = await call_llm_text(
        llm, [SystemMessage(content=prompt)],
        timeout=UTIL_TIMEOUT, label="NL2SQL 生成 SQL",
    )
    if "```" in sql:
        sql = sql.split("```")[1].lstrip("sql").strip()
    return sql

async def search_sql_raw(
    question: str,
    llm: BaseChatModel,
    db: AsyncSession,
)-> tuple[list[dict], str]:
    """NL2SQL：LLM 生成 SQL → 安全校验 → 执行（带重试）→ 返回 (数据, 实际执行的 SQL)。"""
    error_hint = ""
    validated = ""
    for attempt in range(MAX_SQL_RETRIES + 1):
        raw_sql = await _generate_sql(question, llm, error_hint)
        logger.info(f"NL2SQL SQL (attempt {attempt + 1}): {redact_text(raw_sql, 300)}")

        valid, validated = _validate_sql(raw_sql)
        if not valid:
            logger.warning(f"SQL 安全校验失败: {validated}")
            return [], ""

        try:
            # DB 侧超时兜底：asyncio 取消不会终止已下发的查询，避免慢 SQL 占着连接
            await db.execute(
                text(f"SET LOCAL statement_timeout = {int(SQL_TIMEOUT_SECONDS) * 1000}")
            )
            query_result = await asyncio.wait_for(
                db.execute(text(validated)),
                timeout=SQL_TIMEOUT_SECONDS,
            )
            rows = query_result.mappings().all()
            data = [dict(row) for row in rows[:settings.NL2SQL_ROW_LIMIT]]
            break
        except asyncio.TimeoutError:
            logger.warning(f"SQL 执行超时 ({SQL_TIMEOUT_SECONDS}s): {validated}")
            return [], ""
        except Exception as e:
            error_hint = str(e)
            logger.warning(f"SQL 执行失败 (attempt {attempt + 1}): {e}")
            if attempt == MAX_SQL_RETRIES:
                return [], ""
    else:
        return [], ""

    if not data:
        return [], validated

    return data, validated

async def search_sql(
    question: str,
    llm: BaseChatModel,
    db: AsyncSession,
) -> str:
    data, executed_sql = await search_sql_raw(question, llm, db)
    if not data and not executed_sql:
        # 校验被拦截 / 执行失败 / 超时：search_sql_raw 已返回空数据
        data = []
    result_str = json.dumps(data, ensure_ascii=False, indent=2, default=str)
    # 增强生成回答
    prompt = SQL_QA_PROMPT.format(question=question, sql=executed_sql, result=result_str)
    answer = await call_llm_text(
        llm, [SystemMessage(content=prompt)],
        timeout=ANSWER_TIMEOUT, label="NL2SQL 结果解读",
    )
    return answer or "运营数据查询有结果，但解读生成超时，请稍后再问一次。"