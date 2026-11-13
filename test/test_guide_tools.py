# GuideAgent 工具的确定性单测。
#
# 回归点：
# - 就诊准备是规则化清单，必带资料与科室专项都要出现
# - 科室推荐：结构化库没命中时给兜底建议，不编造科室
# - 医院信息缺失时如实说明，绝不编造地址电话
# - 预约/资料整理必须登录，且按 patient_id 隔离

from src.modules.medical.model import Appointment, Department, Hospital, Patient
from src.modules.patient.model import HealthReport, MedicationRecord
from src.tools import guide_tools
from src.tools.runtime import GUEST_HINT

from conftest import FakeDBResult, FakeRuntime, coroutine_of


async def test_visit_preparation_has_common_items():
    out = await coroutine_of(guide_tools.get_visit_preparation)("")
    for item in ("身份证", "医保卡", "过敏史", "症状时间线"):
        assert item in out, f"就诊准备缺少必带项：{item}"


async def test_visit_preparation_adds_department_specific():
    out = await coroutine_of(guide_tools.get_visit_preparation)("心内科")
    assert "心电图" in out  # 心内科专项
    assert "空腹" in out


async def test_visit_preparation_unknown_department_still_usable():
    out = await coroutine_of(guide_tools.get_visit_preparation)("核医学科")
    assert "暂无专项清单" in out or "必带资料" in out


async def test_recommend_department_requires_symptoms():
    out = await coroutine_of(guide_tools.recommend_department)("")
    assert "描述症状" in out


async def test_recommend_department_matches_structured(monkeypatch):
    async def fake_match(keyword, limit=5):
        return [("心内科", "心血管", 3)] if keyword == "胸痛" else []

    monkeypatch.setattr(guide_tools, "_match_departments", fake_match)
    out = await coroutine_of(guide_tools.recommend_department)("胸痛")
    assert "1. 心内科" in out
    assert "胸痛(3)" in out


async def test_recommend_department_falls_back_when_no_match(monkeypatch):
    async def fake_match(keyword, limit=5):
        return []

    monkeypatch.setattr(guide_tools, "_match_departments", fake_match)
    out = await coroutine_of(guide_tools.recommend_department)("不舒服")
    assert "全科" in out or "普通内科" in out


async def test_recommend_department_degrades_on_failure(monkeypatch):
    async def boom(keyword, limit=5):
        raise RuntimeError("db down")

    monkeypatch.setattr(guide_tools, "_match_departments", boom)
    out = await coroutine_of(guide_tools.recommend_department)("头痛")
    assert "暂时不可用" in out


async def test_department_info_requires_name():
    out = await coroutine_of(guide_tools.query_department_info)("")
    assert "科室名称" in out


async def test_department_info_shows_intro_and_diseases(monkeypatch, fake_db):
    async def fake_diseases(_dept, limit=8):
        return ["高血压", "冠心病"]

    monkeypatch.setattr(guide_tools, "_department_diseases", fake_diseases)
    fake_db(
        results=[
            FakeDBResult(scalar=Department(id=1, name="心内科", description="诊治心血管疾病")),
        ]
    )
    out = await coroutine_of(guide_tools.query_department_info)("心内科")
    assert "心内科" in out
    assert "诊治心血管疾病" in out
    assert "高血压" in out


async def test_department_info_unknown_dept(monkeypatch, fake_db):
    async def fake_diseases(_dept, limit=8):
        return []

    monkeypatch.setattr(guide_tools, "_department_diseases", fake_diseases)
    fake_db(FakeDBResult(scalar=None))
    out = await coroutine_of(guide_tools.query_department_info)("外星科")
    assert "暂未收录" in out


async def test_hospital_info_lists_matches(fake_db):
    fake_db(
        FakeDBResult(
            rows=[
                Hospital(
                    name="示例市第一人民医院",
                    level="三级甲等",
                    address="示例路 1 号",
                    phone="0000-12345678",
                    emergency_phone="120",
                    open_hours="08:00-17:30",
                    departments="心内科、骨科",
                )
            ]
        )
    )
    out = await coroutine_of(guide_tools.query_hospital_info)("第一人民医院")
    assert "三级甲等" in out
    assert "示例路 1 号" in out
    assert "120" in out


async def test_hospital_info_never_invents_when_empty(fake_db):
    fake_db(FakeDBResult(rows=[]))
    out = await coroutine_of(guide_tools.query_hospital_info)("不存在的医院")
    assert "暂无匹配" in out
    assert "0000" not in out


async def test_organize_materials_requires_login():
    out = await coroutine_of(guide_tools.organize_visit_materials)(FakeRuntime(user_id="anon"))
    assert GUEST_HINT.split("（")[0] in out


async def test_organize_materials_summarizes(fake_db):
    patient = Patient(id=2, name="张三", gender="男", age=45, allergy_history="青霉素", medical_history="高血压")
    report = HealthReport(patient_id=2, file_name="血常规.pdf", object_key="k", report_type="lab")
    med = MedicationRecord(patient_id=2, drug_name="二甲双胍", dosage="0.5g", status="active")
    appt = Appointment(patient_id=2, department="内分泌科", date="2026-11-10", appointment_no="APT1")
    fake_db(
        results=[
            FakeDBResult(rows=[report]),
            FakeDBResult(rows=[med]),
            FakeDBResult(rows=[appt]),
        ],
        entity=patient,
    )
    out = await coroutine_of(guide_tools.organize_visit_materials)(FakeRuntime(), department="内分泌科")
    assert "张三" in out
    assert "青霉素" in out
    assert "二甲双胍" in out
    assert "血常规.pdf" in out
    assert "APT1" in out


async def test_manage_appointment_requires_login():
    out = await coroutine_of(guide_tools.manage_appointment)("list", FakeRuntime(user_id="anon"))
    assert GUEST_HINT.split("（")[0] in out


async def test_manage_appointment_create_validates(fake_db):
    fake_db()
    out = await coroutine_of(guide_tools.manage_appointment)("create", FakeRuntime(), department="骨科")
    assert "就诊日期" in out


async def test_manage_appointment_create_persists(fake_db):
    db = fake_db()
    out = await coroutine_of(guide_tools.manage_appointment)(
        "create", FakeRuntime(), department="骨科", appointment_date="2026-11-10", time_slot="上午 09:00-10:00"
    )
    assert "已创建预约" in out
    assert db.committed == 1
    assert db.added[0].department == "骨科"
    assert db.added[0].status == "confirmed"


async def test_manage_appointment_cancel_blocks_other_patient(fake_db):
    fake_db(entity=Appointment(patient_id=999, department="骨科", appointment_no="APT9"))
    out = await coroutine_of(guide_tools.manage_appointment)("cancel", FakeRuntime(), appointment_id=1)
    assert "未找到" in out


async def test_manage_appointment_cancel_own(fake_db):
    db = fake_db(entity=Appointment(patient_id=2, department="骨科", appointment_no="APT9"))
    out = await coroutine_of(guide_tools.manage_appointment)("cancel", FakeRuntime(), appointment_id=1)
    assert "已取消预约" in out
    assert db.entity.status == "cancelled"


def test_department_prepare_covers_common_departments():
    """需求里的"就诊准备"要覆盖常见科室，至少要有内外科与慢病相关科室。"""
    assert {"心内科", "内分泌科", "骨科", "儿科"} <= set(guide_tools._DEPARTMENT_PREPARE)
