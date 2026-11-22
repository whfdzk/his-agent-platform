# src/tools/guide_tools.py
#
# GuideAgent（就医辅助）的领域工具：
#   - 科室介绍：诊疗范围 + 该科室常见疾病（来自 departments / diseases）
#   - 科室推荐：症状 → 疾病 → 科室（结构化库兜底，图谱由 Agent 另行检索）
#   - 就诊准备：通用 + 科室相关的准备清单
#   - 就医资料整理：把档案、近期报告、在用药品、预约汇总成随身清单
#   - 医院信息：机构等级/地址/门诊时间/急诊电话
#   - 预约系统对接：查询与创建挂号（落 appointments 表）
#
# 这里只给"就医流程与信息"，不做诊断：是否真的需要就诊由问诊链路判断。

from __future__ import annotations

import re

from langchain_core.tools import tool
from langgraph.prebuilt import ToolRuntime
from loguru import logger
from sqlalchemy import func, or_, select

from src.core.context import UserContext
from src.tools.runtime import (
    GUEST_HINT,
    resolve_patient_id,
    resolve_session_id,
    resolve_thread_id,
    resolve_user_id,
)

# 就诊准备：通用项 + 科室专项，规则化生成（可复现，也便于单测）
_COMMON_PREPARE = [
    "身份证 / 医保卡 / 电子就诊码",
    "既往病历、出院小结、手术记录",
    "近期检查报告与影像原片（CT/MR 建议带电子版）",
    "正在服用的药品清单（最好带药盒或照片）",
    "过敏史与既往病史（写在一张纸上）",
    "症状时间线：什么时候开始、怎样变化、什么会加重或缓解",
]

_DEPARTMENT_PREPARE = {
    "内科": ["空腹 8-12 小时（可能需要抽血）", "近期体温记录"],
    "心内科": ["近期血压/心率记录", "既往心电图与心脏超声报告", "正在服用的降压/抗凝药清单"],
    "内分泌科": ["空腹血糖与餐后血糖记录", "近期糖化血红蛋白报告"],
    "消化内科": ["检查前 1-2 天清淡饮食", "可能需要空腹做胃镜/超声，提前咨询禁食要求"],
    "呼吸内科": ["既往胸片/CT 影像", "肺功能报告（如有）", "吸烟史与接触史说明"],
    "神经内科": ["既往头颅 CT/MR 影像", "头痛或发作的时间、频率记录"],
    "骨科": ["受伤部位影像片", "穿宽松衣物便于查体"],
    "妇产科": ["末次月经时间", "既往 B 超报告", "避开经期（非急诊）"],
    "儿科": ["儿童预防接种证", "生长发育记录", "体温与进食情况记录"],
    "皮肤科": ["皮损部位不要化妆/涂抹药膏，便于观察", "可疑接触物或化妆品清单"],
    "眼科": ["既往验光与眼压记录", "检查当日不要化眼妆"],
    "耳鼻喉科": ["既往听力/鼻内镜检查结果"],
    "中医科": ["近期舌苔照片（自然光下）", "既往中药方"],
}

_MATERIAL_TEMPLATE = "就医资料已整理如下，建议提前 15-30 分钟到院：\n"


async def _match_departments(keyword: str, limit: int = 5):
    """按关键词（症状或科室名）匹配科室，返回 [(科室名, 描述, 命中疾病数)]。"""
    from src.infra.database import AsyncSessionLocal
    from src.modules.medical.model import Department, Disease, DiseaseSymptom, Symptom

    kw = f"%{keyword.strip()}%"
    async with AsyncSessionLocal() as db:
        # 路径 A：症状名 → 疾病 → 科室（分诊推荐的主路径）
        rows = (
            await db.execute(
                select(Department.name, Department.description, func.count(func.distinct(Disease.id)))
                .join(Disease, Disease.department_id == Department.id)
                .join(DiseaseSymptom, DiseaseSymptom.disease_id == Disease.id)
                .join(Symptom, Symptom.id == DiseaseSymptom.symptom_id)
                .where(Symptom.name.like(kw))
                .group_by(Department.id, Department.name, Department.description)
                .order_by(func.count(func.distinct(Disease.id)).desc())
                .limit(limit)
            )
        ).all()
        if rows:
            return [(r[0], r[1], int(r[2])) for r in rows]

        # 路径 B：直接按科室名匹配
        rows = (
            await db.execute(
                select(Department.name, Department.description)
                .where(or_(Department.name.like(kw), Department.description.like(kw)))
                .limit(limit)
            )
        ).all()
        return [(r[0], r[1], 0) for r in rows]


async def _department_diseases(department: str, limit: int = 8) -> list[str]:
    """科室常见疾病（按疾病表里的归属统计，取前 N 个）。"""
    from src.infra.database import AsyncSessionLocal
    from src.modules.medical.model import Department, Disease

    kw = f"%{department.strip()}%"
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(Disease.name)
                .join(Department, Department.id == Disease.department_id)
                .where(Department.name.like(kw))
                .limit(limit)
            )
        ).scalars().all()
    return list(rows)


@tool
async def query_department_info(department: str) -> str:
    """
    查询科室介绍：诊疗范围、科室简介与该科室常见疾病。

    使用时机：
    - "心内科是看什么的"、"骨科都治哪些病"。

    Args:
        department: 科室名称，如"心内科"
    """
    name = (department or "").strip()
    if not name:
        return "请提供科室名称后再查询。"

    from src.infra.database import AsyncSessionLocal
    from src.modules.medical.model import Department

    try:
        async with AsyncSessionLocal() as db:
            row = (
                await db.execute(
                    select(Department).where(Department.name.like(f"%{name}%")).limit(1)
                )
            ).scalar_one_or_none()
        diseases = await _department_diseases(name)
    except Exception as e:
        logger.warning(f"科室介绍查询失败: {type(e).__name__}: {e}")
        return "科室信息服务暂时不可用，请稍后再试。"

    if row is None and not diseases:
        return f"暂未收录「{name}」的科室信息。可尝试更常见的科室名，如内科、外科、心内科、骨科。"

    lines = [f"【{row.name if row else name}｜科室介绍】"]
    if row and row.description:
        lines.append(f"简介：{row.description}")
    else:
        lines.append("简介：该科室的详细简介暂未录入，以下为该科室的常见诊疗范围（来自疾病库）。")
    if diseases:
        lines.append("常见疾病：" + "、".join(diseases))
    lines.append("提示：具体能否在该科室就诊，以医院现场分诊台与挂号系统为准。")
    return "\n".join(lines)


def _split_keywords(text: str) -> list[str]:
    """把口语化症状串切成 2 字以上的候选关键词（最多 5 个，够用且不会拖慢检索）。"""
    return [k for k in re.split(r"[，,、；;。\s]+", text) if len(k) >= 2][:5]


@tool
async def recommend_department(symptoms: str) -> str:
    """
    按症状推荐就诊科室：症状 → 疾病 → 科室 的结构化匹配结果。

    使用时机：
    - "我胸痛该挂什么科"、"头晕挂什么科"。
    注意：这只是分诊参考，急危重症（胸痛、呼吸困难、意识不清等）应立即急诊/120。

    Args:
        symptoms: 用户描述的症状，多个用空格或顿号分隔
    """
    text = (symptoms or "").strip()
    if not text:
        return "请描述症状后再推荐科室。"

    keywords = [k for k in _split_keywords(text)][:5]
    if not keywords:
        return "没有识别到可用于推荐科室的症状关键词，请换个说法描述不适位置与感受。"

    ranked: dict[str, dict] = {}
    try:
        for kw in keywords:
            for dept, desc, hit in await _match_departments(kw):
                entry = ranked.setdefault(dept, {"desc": desc, "score": 0, "hits": []})
                entry["score"] += max(1, hit)
                if hit:
                    entry["hits"].append(f"{kw}({hit})")
    except Exception as e:
        logger.warning(f"科室推荐失败: {type(e).__name__}: {e}")
        return "科室推荐服务暂时不可用，可先挂全科/普通内科由医生分诊。"

    if not ranked:
        return (
            f"未能从症状「{text}」匹配到科室。建议先挂全科医学科或普通内科，"
            "由医生做初步评估后再转诊；急症请直接急诊。"
        )

    ordered = sorted(ranked.items(), key=lambda kv: kv[1]["score"], reverse=True)[:3]
    lines = [f"【科室推荐】症状：{text}"]
    for idx, (dept, info) in enumerate(ordered, 1):
        extra = f"（匹配：{'、'.join(info['hits'][:3])}）" if info["hits"] else ""
        lines.append(f"{idx}. {dept}{extra}")
    lines.append("提示：以上为结构化匹配的分诊参考；症状加重或出现胸痛、呼吸困难、意识改变请立即急诊。")
    return "\n".join(lines)


@tool
async def get_visit_preparation(department: str = "", has_exam: bool = True) -> str:
    """
    生成就诊准备清单：必带资料 + 科室专项准备。

    使用时机：
    - "明天去看心内科要准备什么"、"第一次去医院带什么"。

    Args:
        department: 目标科室（可空），用于追加该科室的专项准备
        has_exam  : 是否可能需要抽血/影像等空腹检查
    """
    lines = ["【就诊准备清单】", "一、必带资料"]
    lines += [f"- {item}" for item in _COMMON_PREPARE]

    if department:
        dept = department.strip()
        matched = [d for d in _DEPARTMENT_PREPARE if d in dept or dept in d]
        if matched:
            lines.append(f"\n二、{dept}专项准备")
            for d in matched:
                lines += [f"- {item}" for item in _DEPARTMENT_PREPARE[d]]
        else:
            lines.append("\n二、专科准备")
            lines.append("- 该科室暂无专项清单，建议提前在医院公众号确认检查前禁食/憋尿等要求")

    if has_exam:
        lines.append("\n三、检查注意")
        lines.append("- 抽血、腹部超声通常需要空腹 8-12 小时，可随身带点食物检查后吃")
        lines.append("- 妇科/泌尿系超声可能需要憋尿，请提前咨询")

    lines.append("\n提示：资料按时间顺序整理成一叠，医生问诊时能更快掌握病情。")
    return "\n".join(lines)


@tool
async def organize_visit_materials(runtime: ToolRuntime[UserContext], department: str = "") -> str:
    """
    整理就医资料：把个人档案、近期报告、在用药品、已有预约汇总成一张随身清单。

    使用时机：
    - "帮我把看病的资料整理一下"、"明天去复诊，我需要带什么"。

    Args:
        department: 就诊科室（可空，用于匹配准备事项）
    """
    patient_id = resolve_patient_id(runtime)
    if patient_id is None:
        return GUEST_HINT + "（就医资料涉及个人档案与报告，需登录后整理。）"

    from src.infra.database import AsyncSessionLocal
    from src.modules.medical.model import Appointment, Patient
    from src.modules.patient.model import HealthReport, MedicationRecord

    try:
        async with AsyncSessionLocal() as db:
            patient = await db.get(Patient, patient_id)
            reports = (
                await db.execute(
                    select(HealthReport)
                    .where(HealthReport.patient_id == patient_id, HealthReport.deleted_at.is_(None))
                    .order_by(HealthReport.created_at.desc())
                    .limit(3)
                )
            ).scalars().all()
            meds = (
                await db.execute(
                    select(MedicationRecord)
                    .where(MedicationRecord.patient_id == patient_id, MedicationRecord.status == "active")
                    .order_by(MedicationRecord.id.desc())
                    .limit(10)
                )
            ).scalars().all()
            appointments = (
                await db.execute(
                    select(Appointment)
                    .where(
                        Appointment.patient_id == patient_id,
                        Appointment.status == "confirmed",
                    )
                    .order_by(Appointment.date.desc())
                    .limit(3)
                )
            ).scalars().all()
    except Exception as e:
        logger.warning(f"就医资料整理失败: {type(e).__name__}: {e}")
        return "就医资料整理服务暂时不可用，请稍后再试。"

    if patient is None:
        return "未查询到患者档案，无法整理就医资料。"

    lines = [_MATERIAL_TEMPLATE]
    lines.append(
        f"一、基本信息：{patient.name}｜{patient.gender or '性别未填'}｜"
        f"{str(patient.age) + '岁' if patient.age else '年龄未填'}｜血型 {patient.blood_type or '未填'}"
    )
    lines.append(f"二、过敏史：{patient.allergy_history or '未填写（就诊时务必口头告知医生）'}")
    lines.append(f"三、既往病史：{patient.medical_history or '未填写'}")

    if meds:
        lines.append("四、在用药品：" + "、".join(
            f"{m.drug_name}{(' ' + m.dosage) if m.dosage else ''}{(' ' + m.frequency) if m.frequency else ''}"
            for m in meds
        ))
    else:
        lines.append("四、在用药品：暂无记录（请现场说明，或先在用药记录里补充）")

    if reports:
        lines.append("五、建议携带的报告：")
        for r in reports:
            lines.append(
                f"- {r.file_name}（{r.report_type}｜{r.report_date or '日期未识别'}｜"
                f"{r.hospital or '机构未填'}）"
            )
    else:
        lines.append("五、报告：暂无已上传报告，如有纸质报告请一并带上")

    if appointments:
        lines.append("六、已有预约：" + "、".join(
            f"{a.department} {a.date or ''} {a.time_slot or ''}（{a.appointment_no}）" for a in appointments
        ))

    if department:
        lines.append(f"\n七、{department}就诊提示：提前确认是否需要空腹、是否需要携带既往影像原片。")
    return "\n".join(lines)


@tool
async def query_hospital_info(name: str = "", city: str = "") -> str:
    """
    查询医院信息：等级、类型、地址、门诊时间、咨询/急诊电话与重点科室。

    使用时机：
    - "XX 医院是三甲吗"、"附近有哪些医院"、"协和医院地址在哪"。

    Args:
        name: 医院名称关键词（可空）
        city: 城市过滤（可空）
    """
    from src.infra.database import AsyncSessionLocal
    from src.modules.medical.model import Hospital

    try:
        async with AsyncSessionLocal() as db:
            q = select(Hospital)
            if name.strip():
                kw = f"%{name.strip()}%"
                q = q.where(or_(Hospital.name.like(kw), Hospital.intro.like(kw)))
            if city.strip():
                q = q.where(Hospital.city.like(f"%{city.strip()}%"))
            rows = (await db.execute(q.order_by(Hospital.id.asc()).limit(5))).scalars().all()
    except Exception as e:
        logger.warning(f"医院信息查询失败: {type(e).__name__}: {e}")
        return "医院信息服务暂时不可用，请稍后再试。"

    if not rows:
        return "医院库中暂无匹配机构。可补充城市或医院全名后再查；也可通过医院官方公众号确认信息。"

    lines = ["【医院信息】"]
    for h in rows:
        lines.append(f"\n{h.name}（{h.level or '等级未标注'}｜{h.hospital_type or '类型未标注'}）")
        if h.address:
            lines.append(f"地址：{h.address}")
        if h.open_hours:
            lines.append(f"门诊时间：{h.open_hours}")
        if h.phone:
            lines.append(f"咨询电话：{h.phone}")
        if h.emergency_phone:
            lines.append(f"急诊电话：{h.emergency_phone}")
        if h.departments:
            lines.append(f"重点科室：{h.departments}")
        if h.intro:
            lines.append(f"简介：{h.intro[:200]}")
    return "\n".join(lines)


@tool
async def manage_appointment(
    action: str,
    runtime: ToolRuntime[UserContext],
    department: str = "",
    appointment_date: str = "",
    time_slot: str = "",
    appointment_id: int = 0,
    diagnosis: str = "",
    doctor_name: str = "",
) -> str:
    """
    就医辅助的预约系统对接：查询、创建、取消挂号预约。

    使用时机：
    - "帮我挂个心内科"、"我有哪些预约"、"把周三的号取消"。
    创建预约前应先与用户确认科室、日期与时段。

    Args:
        action          : list=查询我的预约 / create=创建 / cancel=取消
        department      : 就诊科室（create 必填）
        appointment_date: 就诊日期 YYYY-MM-DD（create 必填）
        time_slot       : 就诊时段，如"上午 09:00-10:00"
        appointment_id  : cancel 时的预约 ID
        diagnosis       : 关联的主诊断/症状（可空）
        doctor_name     : 患者明确点名的医生姓名（可空）。不填时若本科室只有一位在岗医生，
                          直接落到他名下；有多个医生则不指定，按待分配处理。
                          不要自己编造医生姓名，只传患者说过的名字。
    """
    patient_id = resolve_patient_id(runtime)
    if patient_id is None:
        return GUEST_HINT + "（挂号预约需要登录后使用。）"

    act = (action or "list").strip().lower()
    from src.infra.database import AsyncSessionLocal
    from src.modules.medical.model import Appointment

    import uuid

    try:
        async with AsyncSessionLocal() as db:
            if act == "create":
                if not department.strip():
                    return "创建预约需要提供就诊科室。"
                if not appointment_date.strip():
                    return "创建预约需要提供就诊日期（YYYY-MM-DD）。"

                # B2：这里不再直接写 confirmed 预约。
                # 挂号一旦落库就是既成事实，而"先建好、再问患者要不要"正是此前
                # 被投诉的问题（话术里还在问，后台已经占了号源）。
                # 改为生成一条待确认草稿，患者确认后由 executors 真正落库。
                from datetime import date as _date

                from src.modules.clinical import actions as clinical
                from src.modules.clinical.dates import normalize_date
                from src.modules.clinical.link import remember_pending
                from src.modules.clinical.model import ACTION_APPOINTMENT, SOURCE_TOOL

                dept = department.strip()
                # 日期不能让模型心算：实测「明天上午」被写成 2023-11-22。
                # 这里用规则换算，算不出来就要求重新提供，绝不把错误日期带进库。
                date_str, date_err = normalize_date(appointment_date)
                if date_str is None:
                    return (
                        f"{date_err}。今天是 {_date.today().isoformat()}，"
                        "请确认就诊日期后重新发起。"
                    )
                slot = time_slot.strip() or "待分配"
                thread_id = resolve_thread_id(runtime)

                # 医生：C4 续——挂号草稿必须落到具体医生账号上，否则确认后
                # 医生端看不到这个患者（consulted 关系建不出来）。
                # 规则：患者点名的医生按(科室, 姓名)唯一匹配；没点名时只有"本科室唯一在岗医生"
                # 才允许自动指定，多于一位就留空由后端按待分配处理 —— 不猜。
                from src.modules.clinician.profile import (
                    department_id_of,
                    list_directory,
                    resolve_doctor,
                )

                dept_id = await department_id_of(db, dept)
                dept_items = (
                    await list_directory(db, department_id=dept_id) if dept_id else []
                )

                def _label(item: dict) -> str:
                    return f"{item['name']} {item['title']}".strip()

                wanted = doctor_name.strip()
                doctor_id: int | None = None
                doctor_label = ""
                if wanted:
                    doctor_id = await resolve_doctor(db, department=dept, doctor_name=wanted)
                    if doctor_id is None:
                        names = "、".join(
                            (i["name"] + (f"（{i['title']}）" if i["title"] else ""))
                            for i in dept_items
                        ) or "暂无可挂号医生"
                        return (
                            f"本科室未找到医生「{wanted}」。{dept} 当前可选医生：{names}。\n"
                            "请让患者从上面的名单里选择；若患者表示都可以，就不要指定医生再创建。"
                        )
                    hit = next(
                        (i for i in dept_items if i["clinician_id"] == doctor_id), None,
                    )
                    doctor_label = _label(hit) if hit else wanted
                elif len(dept_items) == 1:
                    doctor_id = dept_items[0]["clinician_id"]
                    doctor_label = _label(dept_items[0])

                row = await clinical.create_action(
                    db,
                    action_type=ACTION_APPOINTMENT,
                    payload={
                        "department": dept,
                        "doctor_name": doctor_label,
                        "doctor_clinician_id": doctor_id,
                        "date": date_str,
                        "time_slot": slot,
                        "diagnosis": (diagnosis or "").strip(),
                        "source": "tool:appointment_manager",
                    },
                    summary=(
                        f"{dept}｜{date_str} {slot}"
                        + (f"｜{doctor_label}" if doctor_label else "")
                    ),
                    patient_id=patient_id,
                    user_id=resolve_user_id(runtime),
                    session_id=resolve_session_id(runtime),
                    thread_id=thread_id,
                    source=SOURCE_TOOL,
                    requested_by="tool:appointment_manager",
                    tenant_id=int(
                        getattr(getattr(runtime, "context", None), "tenant_id", 1) or 1
                    ),
                )
                await remember_pending(thread_id, row.id)

                doctor_line = f"就诊医生：{doctor_label}\n" if doctor_label else "就诊医生：待分配\n"
                return (
                    f"已生成挂号申请（参考号 {row.id}）：{dept}｜{date_str} {slot}。\n"
                    f"{doctor_line}"
                    "注意：此时还没有真正挂号，号源未被占用；必须由患者确认后才会落库。\n"
                    "请向用户复述科室、日期、时段与医生，明确问一句「是否确认预约」，"
                    "等用户确认后再告知已成功挂号。"
                )

            if act == "cancel":
                appt = await db.get(Appointment, appointment_id)
                if appt is None or appt.patient_id != patient_id:
                    return f"未找到 ID 为 {appointment_id} 的预约，请先用 action=list 确认。"
                appt.status = "cancelled"
                await db.commit()
                return f"已取消预约：{appt.department}｜{appt.date}（{appt.appointment_no}）。"

            rows = (
                await db.execute(
                    select(Appointment)
                    .where(Appointment.patient_id == patient_id, Appointment.status == "confirmed")
                    .order_by(Appointment.date.desc())
                    .limit(10)
                )
            ).scalars().all()
            if not rows:
                return "暂无进行中的预约。需要挂号时请告知科室与日期，我来帮您创建。"
            lines = ["【我的预约】"]
            for a in rows:
                lines.append(
                    f"- ID {a.id}｜{a.department}｜{a.date or '日期待定'} {a.time_slot or ''}"
                    f"｜{a.appointment_no}｜{a.doctor_name or '医生待分配'}"
                )
            return "\n".join(lines)
    except Exception as e:
        logger.warning(f"预约操作失败 action={act}: {type(e).__name__}: {e}")
        return "预约服务暂时不可用，请稍后再试；紧急就诊请直接前往急诊。"
