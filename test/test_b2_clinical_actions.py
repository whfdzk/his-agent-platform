"""B2 回归测试：预约日期规范化 + clinical_actions 状态机。

不依赖 pytest-asyncio（当前环境未装该插件），异步逻辑统一在一次 asyncio.run 里跑完，
避免多个 event loop 复用同一个连接池导致的 "attached to a different loop"。

用例会真实落库，结束时按 session_id 清理自己造的动作与预约，不污染开发库。
"""

import asyncio
import uuid
from datetime import date

from src.modules.clinical.dates import normalize_date


def test_normalize_date_relative_and_weekday():
    today = date(2026, 11, 20)  # 周五

    assert normalize_date("明天上午", today=today) == ("2026-11-21", "")
    assert normalize_date("今天下午", today=today) == ("2026-11-20", "")
    assert normalize_date("后天", today=today) == ("2026-11-22", "")
    assert normalize_date("大后天上午 9 点", today=today) == ("2026-11-23", "")
    # 今天是周五，"周三"指下周三（本周三已过）
    assert normalize_date("周三", today=today) == ("2026-11-25", "")
    assert normalize_date("下周三上午", today=today) == ("2026-11-25", "")
    assert normalize_date("本周五", today=today) == ("2026-11-20", "")
    assert normalize_date("星期日", today=today) == ("2026-11-22", "")


def test_normalize_date_absolute_and_rejects_past():
    today = date(2026, 11, 20)

    assert normalize_date("2026-11-25", today=today) == ("2026-11-25", "")
    assert normalize_date("2026/11/25", today=today) == ("2026-11-25", "")
    assert normalize_date("11月25日", today=today) == ("2026-11-25", "")

    # 模型把「明天」算成 2023-11-22 的那次事故，必须被挡下
    iso, err = normalize_date("2023-11-22", today=today)
    assert iso is None
    assert "过去" in err

    iso, err = normalize_date("随便哪天都行", today=today)
    assert iso is None and err


def test_clinical_action_state_machine():
    asyncio.run(_state_machine_case())


async def _state_machine_case() -> None:
    from sqlalchemy import delete, func, select

    from src.infra.database import AsyncSessionLocal
    from src.modules.clinical import actions as clinical
    from src.modules.clinical.executors import executor_for
    from src.modules.clinical.model import ClinicalAction
    from src.modules.medical.model import Appointment

    sid = f"pytest-b2-{uuid.uuid4().hex[:8]}"
    tid = f"1:{sid}"
    payload = {
        "department": "心内科",
        "date": "2026-11-25",
        "time_slot": "上午 09:00-10:00",
        "diagnosis": "pytest",
    }

    async def count_confirmed() -> int:
        async with AsyncSessionLocal() as db:
            return (
                await db.execute(
                    select(func.count())
                    .select_from(Appointment)
                    .where(Appointment.status == "confirmed")
                )
            ).scalar_one()

    before = await count_confirmed()

    try:
        async with AsyncSessionLocal() as db:
            draft = await clinical.create_action(
                db, action_type="appointment", payload=dict(payload),
                summary="pytest 草稿", patient_id=1, user_id="1",
                session_id=sid, thread_id=tid, source="tool", requested_by="pytest",
            )
            draft_id = draft.id
        assert draft.status == "pending_confirm"
        assert await count_confirmed() == before, "草稿阶段不应该建预约"

        async with AsyncSessionLocal() as db:
            executed, data = await clinical.approve(
                db, draft_id, actor="patient:1", executor=executor_for("appointment"),
            )
        assert executed.status == "approved"
        assert data["appointment_no"]
        assert await count_confirmed() == before + 1

        async with AsyncSessionLocal() as db:
            again, _ = await clinical.approve(
                db, draft_id, actor="patient:1", executor=executor_for("appointment"),
            )
        assert again.status == "approved"
        assert await count_confirmed() == before + 1, "重复确认必须幂等"

        async with AsyncSessionLocal() as db:
            to_reject = await clinical.create_action(
                db, action_type="appointment", payload=dict(payload),
                summary="pytest 拒绝", patient_id=1, user_id="1",
                session_id=sid, thread_id=tid, source="tool", requested_by="pytest",
            )
            reject_id = to_reject.id
        async with AsyncSessionLocal() as db:
            rejected = await clinical.reject(
                db, reject_id, actor="patient:1", reason="pytest 拒绝",
            )
        assert rejected.status == "rejected"
        assert await count_confirmed() == before + 1

        async with AsyncSessionLocal() as db:
            stale = await clinical.create_action(
                db, action_type="appointment", payload=dict(payload),
                summary="pytest 过期", patient_id=1, user_id="1",
                session_id=sid, thread_id=tid, source="tool", requested_by="pytest",
                ttl_seconds=60,
            )
            stale_id = stale.id
            row = (
                await db.execute(select(ClinicalAction).where(ClinicalAction.id == stale_id))
            ).scalar_one()
            from datetime import datetime, timedelta, timezone
            row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=5)
            await db.commit()

        try:
            async with AsyncSessionLocal() as db:
                await clinical.approve(
                    db, stale_id, actor="patient:1", executor=executor_for("appointment"),
                )
        except clinical.ActionError as e:
            assert e.code == "expired"
        else:
            raise AssertionError("过期动作应该抛 ActionError('expired')")

        assert await count_confirmed() == before + 1, "过期动作不应该落预约"
    finally:
        async with AsyncSessionLocal() as db:
            await db.execute(delete(ClinicalAction).where(ClinicalAction.session_id == sid))
            await db.execute(delete(Appointment).where(Appointment.session_id == sid))
            await db.commit()
        # 同进程内其他测试会起新的 event loop：先归还本 loop 的连接
        from src.infra.database import engine

        await engine.dispose()
