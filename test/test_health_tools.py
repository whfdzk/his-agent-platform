# HealthManageAgent 工具的确定性单测。
#
# 回归点：
# - 血压缺舒张压必须拒绝记录（否则会存进一条无法评估的记录）
# - 趋势图与周期报告是纯函数，可脱离数据库验证
# - 游客不能写任何健康数据
# - 提醒 / 计划的停用、完成要按 patient_id 隔离

from datetime import date

from src.modules.patient.service import build_period_report, build_trend_chart
from src.tools import health_tools
from src.tools.runtime import GUEST_HINT

from conftest import FakeDBResult, FakeRuntime, coroutine_of


def _points(values):
    return [{"t": f"2026-11-{i + 1:02d} 08:00", "primary": v, "level": "normal"} for i, v in enumerate(values)]


def test_trend_chart_rising():
    out = build_trend_chart(_points([120, 125, 130, 140]), unit="mmHg")
    assert "上升" in out
    assert "140" in out and "120" in out
    # 折线本体：8 个方块字符档位之一
    assert any(ch in out for ch in "▁▂▃▄▅▆▇█")


def test_trend_chart_empty():
    assert "暂无" in build_trend_chart([], unit="mmHg")


def test_trend_chart_samples_long_series():
    out = build_trend_chart(_points(list(range(100))), unit="kg", width=10)
    assert out.count("\n") == 1  # 只有折线行与统计行


def test_period_report_empty_period():
    summary, content, stats = build_period_report("week", date(2026, 11, 1), date(2026, 11, 7), [], [], [])
    assert "暂无" in summary
    assert stats["record_count"] == 0
    assert "建议" in content


def test_period_report_aggregates_metrics():
    metrics = [
        {"metric_type": "blood_pressure", "primary": 130, "secondary": 85, "unit": "mmHg", "level": "high"},
        {"metric_type": "blood_pressure", "primary": 120, "secondary": 80, "unit": "mmHg", "level": "normal"},
        {"metric_type": "weight", "primary": 65, "unit": "kg", "level": "normal"},
    ]
    summary, content, stats = build_period_report(
        "week",
        date(2026, 11, 1),
        date(2026, 11, 7),
        metrics,
        [{"title": "每日 6000 步", "status": "active", "target_text": "3 个月减重 5kg"}],
        [{"drug_name": "二甲双胍", "dosage": "0.5g", "frequency": "每日两次", "status": "active"}],
    )
    assert stats["record_count"] == 3
    assert stats["abnormal_count"] == 1
    assert stats["active_medications"] == 1
    assert stats["metrics"]["blood_pressure"]["avg"] == 125.0
    assert "血压" in content and "二甲双胍" in content and "每日 6000 步" in content
    assert "异常 1 次" in summary


async def test_record_metric_requires_login():
    out = await coroutine_of(health_tools.record_vital_metric)(
        "blood_glucose", 7.2, FakeRuntime(user_id="anon")
    )
    assert GUEST_HINT.split("（")[0] in out


async def test_record_blood_pressure_needs_secondary(fake_db):
    fake_db()
    out = await coroutine_of(health_tools.record_vital_metric)(
        "blood_pressure", 130, FakeRuntime()
    )
    assert "舒张压" in out


async def test_record_unsupported_metric(fake_db):
    fake_db()
    out = await coroutine_of(health_tools.record_vital_metric)("血氧", 98, FakeRuntime())
    assert "暂不支持" in out


async def test_record_metric_persists_and_evaluates(fake_db):
    db = fake_db()
    out = await coroutine_of(health_tools.record_vital_metric)(
        "blood_pressure", 150, FakeRuntime(), value_secondary=95
    )
    assert "已记录血压" in out
    assert "偏高" in out
    assert db.committed == 1
    assert db.added[0].level == "high"


async def test_record_glucose_critical_hint(fake_db):
    db = fake_db()
    out = await coroutine_of(health_tools.record_vital_metric)("blood_glucose", 17.5, FakeRuntime())
    assert "危急" in out
    assert db.added[0].level == "critical"


async def test_trends_requires_login():
    out = await coroutine_of(health_tools.get_metric_trends)(FakeRuntime(user_id="anon"))
    assert GUEST_HINT.split("（")[0] in out


async def test_trends_no_data(fake_db):
    fake_db(FakeDBResult(rows=[]))
    out = await coroutine_of(health_tools.get_metric_trends)(FakeRuntime(), metric_type="weight", days=7)
    assert "没有" in out


async def test_trends_renders_chart(fake_db):
    from src.modules.patient.model import VitalMetric

    rows = [
        VitalMetric(patient_id=2, metric_type="weight", value_primary=65, unit="kg", level="normal"),
        VitalMetric(patient_id=2, metric_type="weight", value_primary=64, unit="kg", level="normal"),
    ]
    fake_db(FakeDBResult(rows=rows))
    out = await coroutine_of(health_tools.get_metric_trends)(FakeRuntime(), metric_type="weight")
    assert "体重" in out
    assert "最近一次" in out


async def test_plan_requires_login():
    out = await coroutine_of(health_tools.manage_health_plan)("add", FakeRuntime(user_id="anon"), "每日快走")
    assert GUEST_HINT.split("（")[0] in out


async def test_plan_add(fake_db):
    db = fake_db()
    out = await coroutine_of(health_tools.manage_health_plan)(
        "add", FakeRuntime(), "每日快走 30 分钟", "exercise", "3 个月减重 5kg", "每日"
    )
    assert "已建立健康计划" in out
    assert db.added[0].plan_type == "exercise"
    assert db.added[0].status == "active"


async def test_plan_add_requires_title(fake_db):
    fake_db()
    out = await coroutine_of(health_tools.manage_health_plan)("add", FakeRuntime(), "")
    assert "计划标题" in out


async def test_plan_done_blocks_other_patient(fake_db):
    from src.modules.patient.model import HealthPlan

    fake_db(entity=HealthPlan(patient_id=999, title="别人的计划"))
    out = await coroutine_of(health_tools.manage_health_plan)("done", FakeRuntime(), plan_id=1)
    assert "未找到" in out


async def test_reminder_add(fake_db):
    db = fake_db()
    out = await coroutine_of(health_tools.manage_reminder)(
        "add", FakeRuntime(), "测量血压", "07:30"
    )
    assert "已创建提醒" in out
    assert db.added[0].remind_time == "07:30"
    assert db.added[0].frequency == "daily"


async def test_reminder_toggle_blocks_other_patient(fake_db):
    from src.modules.patient.model import HealthReminder

    fake_db(entity=HealthReminder(patient_id=999, title="别人的提醒"))
    out = await coroutine_of(health_tools.manage_reminder)("off", FakeRuntime(), reminder_id=1)
    assert "未找到" in out


async def test_period_report_requires_login():
    out = await coroutine_of(health_tools.generate_period_report)(FakeRuntime(user_id="anon"))
    assert GUEST_HINT.split("（")[0] in out


async def test_period_report_invalid_period(fake_db):
    fake_db()
    out = await coroutine_of(health_tools.generate_period_report)(FakeRuntime(), period_type="year")
    assert "week" in out and "month" in out


async def test_period_report_generates_and_persists(fake_db):
    from src.modules.patient.model import HealthPlan, MedicationRecord, VitalMetric

    db = fake_db(
        results=[
            FakeDBResult(rows=[VitalMetric(patient_id=2, metric_type="weight", value_primary=65, unit="kg", level="normal")]),
            FakeDBResult(rows=[HealthPlan(patient_id=2, title="每日 6000 步", status="active")]),
            FakeDBResult(rows=[MedicationRecord(patient_id=2, drug_name="二甲双胍", status="active")]),
        ]
    )
    out = await coroutine_of(health_tools.generate_period_report)(FakeRuntime(), period_type="week")
    assert db.committed == 1
    assert db.added[0].period_type == "week"
    assert db.added[0].stats["record_count"] == 1
    assert "本周期共" in out
