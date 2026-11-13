# NL2SQL 安全校验的确定性单测。
# 这是 LLM 生成 SQL 直连数据库的唯一闸门，必须有回归保护：
# 新增放行规则前请先确认不会泄漏 PHI。
#
# 本轮收紧后的契约：
# 1. 只有白名单表可查（运营分析用到的 7 张业务表）；
# 2. patients 整表禁止进入 NL2SQL（`SELECT name FROM patients` 泄漏姓名，
#    `count(*)` 也统一走专门的统计接口，不在模型生成 SQL 的能力范围内）；
# 3. 单条语句、禁止注释与系统目录，LIMIT 强制 ≤100。

import pytest

from src.knowledge.nl2sql import _validate_sql


# ── 允许：白名单表 + 自动补齐 LIMIT ───────────────────────────────────────
@pytest.mark.parametrize("sql", [
    "SELECT name FROM departments",
    "SELECT d.name FROM departments d WHERE d.id > 1",
    "SELECT d.name, s.name FROM diseases d JOIN symptoms s ON s.id = d.id",
])
def test_plain_select_is_allowed_and_appends_limit(sql: str):
    valid, normalized = _validate_sql(sql)
    assert valid
    assert normalized.rstrip().endswith("LIMIT 100")


def test_existing_limit_is_not_duplicated():
    valid, normalized = _validate_sql("SELECT name FROM departments LIMIT 10")
    assert valid
    assert normalized == "SELECT name FROM departments LIMIT 10"


def test_trailing_semicolon_is_stripped():
    valid, normalized = _validate_sql("SELECT name FROM departments;")
    assert valid
    assert ";" not in normalized
    assert normalized.rstrip().endswith("LIMIT 100")


def test_oversized_limit_is_clamped():
    valid, normalized = _validate_sql("SELECT name FROM departments LIMIT 5000")
    assert valid
    assert "LIMIT 100" in normalized.upper()
    assert "5000" not in normalized


# ── 拒绝：非白名单表（患者数据与系统对象）────────────────────────────────
@pytest.mark.parametrize("sql", [
    "SELECT count(*) FROM patients",          # 患者表整体不开放给模型生成 SQL
    "SELECT name FROM patients",
    "SELECT * FROM patients",
    "SELECT * FROM patients_visits",          # 未登记的表同样拒绝
    "SELECT * FROM doctors",                  # 未进入白名单
])
def test_non_whitelisted_tables_are_rejected(sql: str):
    valid, message = _validate_sql(sql)
    assert not valid
    assert "不允许查询表" in message


@pytest.mark.parametrize("sql", [
    "SELECT * FROM admins",                   # password_hash 所在地
    "SELECT * FROM information_schema.tables",
    "SELECT * FROM pg_catalog.pg_tables",
])
def test_system_objects_are_rejected_lists(sql: str):
    valid, message = _validate_sql(sql)
    assert not valid
    # 这两类对象先命中禁止清单，错误信息会强调"禁止的操作或对象"
    assert "禁止" in message


# ── 拒绝：写操作与 DDL ────────────────────────────────────────────────────
@pytest.mark.parametrize("sql", [
    "DELETE FROM patients WHERE id = 1",
    "INSERT INTO patients (name) VALUES ('x')",
    "UPDATE patients SET name = 'x'",
    "DROP TABLE patients",
    "TRUNCATE TABLE patients",
    "ALTER TABLE patients ADD COLUMN x int",
    "GRANT ALL ON patients TO public",
    "EXPLAIN SELECT * FROM patients",
])
def test_non_select_statements_are_rejected(sql: str):
    valid, message = _validate_sql(sql)
    assert not valid
    assert message == "只允许 SELECT 查询"


def test_write_hidden_behind_select_is_rejected():
    # 堆叠语句：多语句校验先兜住，绝不能只执行前半段
    valid, _ = _validate_sql("SELECT name FROM departments; DELETE FROM patients")
    assert not valid


@pytest.mark.parametrize("sql", [
    "SELECT name FROM departments -- comment",
    "SELECT name FROM departments /* comment */",
    "SELECT pg_sleep(5)",
])
def test_comments_and_dangerous_functions_are_rejected(sql: str):
    valid, _ = _validate_sql(sql)
    assert not valid
