"""B3 回归测试：急症分级事件的归一化、落库、查询与聚合。

不依赖 pytest-asyncio（环境未装该插件），异步逻辑统一在一次 asyncio.run 内跑完，
避免连接池跨 event loop 复用。测试数据按 session_id 前缀自清理。
"""

import asyncio
import uuid


def test_normalize_level():
    from src.modules.clinical.events import normalize_level, severity, is_emergency_level

    assert normalize_level("red") == "red"
    assert normalize_level("RED") == "red"
    assert normalize_level("橙") == "orange"
    assert normalize_level("中") == "yellow"
    assert normalize_level("green") == "green"
    assert normalize_level("normal") == "green"
    # 认不出来按最重处理：宁可多提示一次急诊
    assert normalize_level("说不清") == "red"
    assert normalize_level(None) == "green"

    assert is_emergency_level("red") and is_emergency_level("yellow")
    assert not is_emergency_level("green")
    assert severity("red") > severity("orange") > severity("yellow") > severity("green")


def test_emergency_event_persist_and_query():
    asyncio.run(_event_case())


async def _event_case() -> None:
    from sqlalchemy import delete

    from src.infra.database import AsyncSessionLocal
    from src.modules.clinical import events as ev
    from src.modules.clinical.model import ClinicalEvent

    sid = f"pytest-b3-{uuid.uuid4().hex[:8]}"

    try:
        async with AsyncSessionLocal() as db:
            # green 不落库：没有风险就不该产生记录
            assert await ev.record_emergency(level="green", reason="无红旗", session_id=sid) is None

            rec = await ev.record_emergency(
                level="red",
                reason="胸痛伴冷汗",
                symptoms=["胸痛", "冷汗", "胸痛"],
                patient_id=1,
                user_id="1",
                session_id=sid,
                thread_id=f"1:{sid}",
                payload={"round": 0, "raw_level": "red"},
            )
            assert rec is not None
            assert rec.level == "red"

            # 症状词去重
            assert rec.symptoms == ["胸痛", "冷汗"]

        async with AsyncSessionLocal() as db:
            items = await ev.list_events(db, session_id=sid, limit=20)
            assert len(items) == 1
            assert items[0]["level"] == "red"
            assert items[0]["level_label"] == "危急"
            assert items[0]["symptoms"] == ["胸痛", "冷汗"]
            # 对外结构不外泄 payload
            assert "payload" not in items[0]

            # min_level 过滤：orange 及以上才返回，red 命中
            reds = await ev.list_events(db, session_id=sid, min_level="orange")
            assert len(reds) == 1
            # level 精确过滤：yellow 不该命中
            yellows = await ev.list_events(db, session_id=sid, level="yellow")
            assert yellows == []

            counts = await ev.count_by_level(db, session_id=sid)
            assert counts["red"] == 1
            assert set(counts) == {"red", "orange", "yellow", "green"}
            assert counts["green"] == 0

            # 患者维度过滤也能命中
            mine = await ev.list_events(db, patient_id=1, session_id=sid)
            assert len(mine) == 1
    finally:
        async with AsyncSessionLocal() as db:
            await db.execute(delete(ClinicalEvent).where(ClinicalEvent.session_id == sid))
            await db.commit()
        # 同一进程里还有别的测试会再起一个 event loop：先把本 loop 的连接还掉，
        # 否则下一个 asyncio.run 复用池内连接会报 "attached to a different loop"
        from src.infra.database import engine

        await engine.dispose()
