"""C4 回归测试：医护可见范围（科室绑定 + 责任患者）。

不依赖 pytest-asyncio：所有异步逻辑在一次 asyncio.run 内完成，末尾 engine.dispose()
避免同进程内后续测试的 event loop 复用连接池时报错。测试数据自清理。
"""

import asyncio
import uuid


def test_scope_rules():
    asyncio.run(_scope_case())


async def _scope_case() -> None:
    from sqlalchemy import delete, func, select

    from src.infra.database import AsyncSessionLocal, engine
    from src.modules.clinician import scope as cscope
    from src.modules.clinician.model import ClinicianDepartment, ClinicianPatient
    from src.modules.medical.model import Appointment

    clinician_id = 999901      # 测试专用临时账号：不能用 4 号真实医护，
                               # 否则 cleanup 会把界面种子/真实接诊建立的关系一起删掉
    dept_id = 3               # 心内科
    responsible_patient = 1   # 责任患者
    other_patient = 3         # 非责任患者
    appt_no = f"PYTC4{uuid.uuid4().hex[:8].upper()}"

    try:
        async with AsyncSessionLocal() as db:
            await cscope.link_department(
                db, clinician_id=clinician_id, department_id=dept_id,
                is_primary=True, granted_by="pytest",
            )
            # 幂等：再调一次不应新增行
            await cscope.link_department(
                db, clinician_id=clinician_id, department_id=dept_id,
                is_primary=True, granted_by="pytest",
            )
            await cscope.link_patient(
                db, clinician_id=clinician_id, patient_id=responsible_patient,
                relation="assigned", granted_by="pytest",
            )
            await cscope.link_patient(
                db, clinician_id=clinician_id, patient_id=responsible_patient,
                relation="assigned", granted_by="pytest",
            )

            db.add(Appointment(
                patient_id=other_patient,
                appointment_no=appt_no,
                doctor_name="测试医生",
                department="心内科",
                date="2026-11-19",
                time_slot="上午 09:00-10:00",
                fee=30,
                queue_number=1,
                status="confirmed",
            ))
            await db.commit()

        async with AsyncSessionLocal() as db:
            dup = (
                await db.execute(
                    select(func.count()).select_from(ClinicianPatient).where(
                        ClinicianPatient.clinician_id == clinician_id,
                        ClinicianPatient.patient_id == responsible_patient,
                    )
                )
            ).scalar_one()
            assert dup == 1, "责任关系应幂等"
            dup_dept = (
                await db.execute(
                    select(func.count()).select_from(ClinicianDepartment).where(
                        ClinicianDepartment.clinician_id == clinician_id,
                        ClinicianDepartment.department_id == dept_id,
                    )
                )
            ).scalar_one()
            assert dup_dept == 1, "科室绑定应幂等"

            scope = await cscope.build_scope(db, clinician_id)
            assert "心内科" in scope.department_names, scope.department_names
            assert responsible_patient in scope.patient_ids
            assert other_patient not in scope.patient_ids

            # 责任患者：yellow 也可读（诊疗必需）
            ok, why = cscope.can_read_event(
                scope, patient_id=responsible_patient, level="yellow",
                patient_departments={"骨科"},
            )
            assert ok and why == cscope.REASON_RESPONSIBLE, (ok, why)

            # 非责任患者 + 本科室 + red → 科室告警可见
            ok, why = cscope.can_read_event(
                scope, patient_id=other_patient, level="red",
                patient_departments={"心内科"},
            )
            assert ok and why == cscope.REASON_DEPARTMENT, (ok, why)

            # 非责任患者 + 本科室 + yellow → 不可见（科室只广播高危两档）
            ok, why = cscope.can_read_event(
                scope, patient_id=other_patient, level="yellow",
                patient_departments={"心内科"},
            )
            assert not ok and why == cscope.REASON_DENIED, (ok, why)

            # 非责任患者 + 别的科室 + red → 不可见
            ok, _ = cscope.can_read_event(
                scope, patient_id=other_patient, level="red",
                patient_departments={"骨科"},
            )
            assert not ok

            # 游客事件（patient_id=None）一律不可见
            ok, _ = cscope.can_read_event(
                scope, patient_id=None, level="red", patient_departments={"心内科"},
            )
            assert not ok

            # 批量取最近科室
            depts = await cscope.recent_patient_departments(db, [other_patient])
            assert depts.get(other_patient) == {"心内科"}, depts

            # 没有任何绑定的医生：scope 为空，一切拒绝
            empty = await cscope.build_scope(db, 999999)
            assert empty.department_names == frozenset() and empty.patient_ids == frozenset()
            ok, _ = cscope.can_read_event(
                empty, patient_id=responsible_patient, level="red",
                patient_departments={"心内科"},
            )
            assert not ok
    finally:
        async with AsyncSessionLocal() as db:
            await db.execute(delete(Appointment).where(Appointment.appointment_no == appt_no))
            await db.execute(
                delete(ClinicianPatient).where(
                    ClinicianPatient.clinician_id == clinician_id,
                    ClinicianPatient.patient_id == responsible_patient,
                )
            )
            await db.execute(
                delete(ClinicianDepartment).where(
                    ClinicianDepartment.clinician_id == clinician_id,
                    ClinicianDepartment.department_id == dept_id,
                )
            )
            await db.commit()
        await engine.dispose()
