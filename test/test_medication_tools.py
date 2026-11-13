# MedicationAgent 药品工具的确定性单测。
#
# 回归点：
# - 说明书按章节输出，字段缺失时明确写"未收录"，不允许编造
# - 库里没有该药时退回知识库，仍没有则如实说明
# - 过敏风险：游客拒绝、命中过敏原与未命中两种分支
# - 用药记录：游客拒绝、新增落库、停用按 patient_id 隔离

import pytest

from src.tools import medication_tools
from src.tools.runtime import GUEST_HINT

from conftest import FakeDBResult, FakeRuntime, coroutine_of

DRUG = {
    "name": "布洛芬缓释胶囊",
    "alias": "芬必得",
    "category": "解热镇痛",
    "manufacturer": "示例制药",
    "approval_number": "国药准字H00000000",
    "is_otc": True,
    "ingredient": "布洛芬",
    "indication": "缓解轻至中度疼痛",
    "usage_dosage": "口服，一次 1 粒，一日 2 次",
    "adverse_reaction": "偶见恶心、胃部不适",
    "contraindication": "活动性消化道溃疡患者禁用",
    "precaution": "不得同时服用其它解热镇痛药",
    "interaction": "与抗凝药合用增加出血风险",
    "storage": "密封保存",
    "full_instruction": "【成分】布洛芬 0.3g",
}


def _patched_drug(monkeypatch, drug):
    async def fake_load(_name):
        return drug

    monkeypatch.setattr(medication_tools, "_load_drug", fake_load)


async def test_query_drug_info_outputs_all_sections(monkeypatch):
    _patched_drug(monkeypatch, DRUG)
    out = await coroutine_of(medication_tools.query_drug_info)("布洛芬")

    assert "布洛芬缓释胶囊" in out
    for label in ("主要成分", "适应症", "用法用量", "不良反应", "禁忌", "注意事项", "药物相互作用"):
        assert label in out, f"说明书缺少章节：{label}"


async def test_query_drug_info_single_section(monkeypatch):
    _patched_drug(monkeypatch, DRUG)
    out = await coroutine_of(medication_tools.query_drug_info)("布洛芬", section="adverse")
    assert "不良反应" in out
    assert "用法用量" not in out


async def test_query_drug_info_marks_missing_field(monkeypatch):
    drug = dict(DRUG, ingredient=None, full_instruction="")
    _patched_drug(monkeypatch, drug)
    out = await coroutine_of(medication_tools.query_drug_info)("布洛芬")
    assert "主要成分：（说明书未收录）" in out


async def test_query_drug_info_falls_back_to_knowledge_base(monkeypatch):
    _patched_drug(monkeypatch, None)

    async def fake_docs(_name, _section):
        return "知识库：布洛芬禁用于活动性溃疡。"

    monkeypatch.setattr(medication_tools, "_search_instruction_docs", fake_docs)
    out = await coroutine_of(medication_tools.query_drug_info)("布洛芬")
    assert "知识库检索结果" in out


async def test_query_drug_info_unknown_drug_never_invents(monkeypatch):
    _patched_drug(monkeypatch, None)

    async def fake_docs(_name, _section):
        return ""

    monkeypatch.setattr(medication_tools, "_search_instruction_docs", fake_docs)
    out = await coroutine_of(medication_tools.query_drug_info)("不存在的药")
    assert "暂未收录" in out
    assert "不要凭记忆编造" in out


async def test_allergy_risk_requires_login():
    out = await coroutine_of(medication_tools.check_drug_allergy_risk)(
        "布洛芬", FakeRuntime(user_id="anon")
    )
    assert GUEST_HINT.split("（")[0] in out


async def test_allergy_risk_hit(monkeypatch, fake_db):
    _patched_drug(monkeypatch, DRUG)
    fake_db(FakeDBResult(scalar="青霉素，头孢"))

    # 命中分支：过敏原出现在成分文本里（DRUG.ingredient="布洛芬" 不命中，
    # 这里用含"青霉素"的药品验证命中路径）
    async def fake_load(_name):
        return dict(DRUG, name="青霉素V钾片", ingredient="青霉素V钾")

    monkeypatch.setattr(medication_tools, "_load_drug", fake_load)
    out = await coroutine_of(medication_tools.check_drug_allergy_risk)(
        "青霉素V钾片", FakeRuntime()
    )
    assert "命中关联" in out
    assert "青霉素" in out


async def test_allergy_risk_no_hit(monkeypatch, fake_db):
    _patched_drug(monkeypatch, DRUG)
    fake_db(FakeDBResult(scalar="海鲜"))
    out = await coroutine_of(medication_tools.check_drug_allergy_risk)("布洛芬", FakeRuntime())
    assert "未" in out and "匹配到" in out


async def test_allergy_risk_without_allergy_record(monkeypatch, fake_db):
    fake_db(FakeDBResult(scalar=None))
    out = await coroutine_of(medication_tools.check_drug_allergy_risk)("布洛芬", FakeRuntime())
    assert "暂无过敏史" in out


async def test_medication_record_requires_login():
    out = await coroutine_of(medication_tools.manage_medication_record)(
        "add", FakeRuntime(user_id="anon"), "二甲双胍"
    )
    assert GUEST_HINT.split("（")[0] in out


async def test_medication_record_add_persists(fake_db):
    db = fake_db()
    out = await coroutine_of(medication_tools.manage_medication_record)(
        "add", FakeRuntime(), "二甲双胍", "0.5g", "每日两次", "降糖"
    )
    assert "已记录用药：二甲双胍" in out
    assert db.committed == 1
    assert db.added[0].drug_name == "二甲双胍"
    assert db.added[0].status == "active"


async def test_medication_record_add_requires_drug_name(fake_db):
    fake_db()
    out = await coroutine_of(medication_tools.manage_medication_record)("add", FakeRuntime(), "")
    assert "需要提供药品名称" in out


async def test_medication_record_list_shows_records(fake_db):
    from src.modules.patient.model import MedicationRecord

    record = MedicationRecord(patient_id=2, drug_name="二甲双胍", status="active")
    fake_db(FakeDBResult(rows=[record]))
    out = await coroutine_of(medication_tools.manage_medication_record)("list", FakeRuntime())
    assert "【用药记录】" in out
    assert "二甲双胍" in out


async def test_medication_record_stop_blocks_other_patient(fake_db):
    from src.modules.patient.model import MedicationRecord

    fake_db(FakeDBResult(), entity=MedicationRecord(patient_id=999, drug_name="别人的药"))
    out = await coroutine_of(medication_tools.manage_medication_record)(
        "stop", FakeRuntime(), record_id=1
    )
    assert "未找到" in out


async def test_medication_record_service_failure_degrades(monkeypatch, fake_db):
    from src.infra import database

    class Boom:
        def __call__(self):
            raise RuntimeError("db down")

    monkeypatch.setattr(database, "AsyncSessionLocal", Boom())
    out = await coroutine_of(medication_tools.manage_medication_record)("list", FakeRuntime())
    assert "暂时不可用" in out


def test_section_fields_cover_required_capabilities():
    """需求要求的章节必须都在：成分 / 禁忌 / 不良反应 / 相互作用。"""
    assert {"ingredient", "contraindication", "adverse", "interaction"} <= set(
        medication_tools.SECTION_FIELDS
    )


@pytest.mark.parametrize("section", ["ingredient", "adverse", "interaction"])
async def test_query_drug_info_section_labels(section, monkeypatch):
    _patched_drug(monkeypatch, DRUG)
    out = await coroutine_of(medication_tools.query_drug_info)("布洛芬", section=section)
    assert medication_tools.SECTION_FIELDS[section][0] in out
