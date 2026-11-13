# src/tools/health_tools.py
#
# HealthManageAgent（慢病与健康指标管理）的领域工具：
#   - 体征记录：血压 / 血糖 / 体重 / 心率
#   - 趋势图：时序 + 文本折线（对话渠道可直接展示）
#   - 健康计划：长期目标的建立与跟踪
#   - 提醒：测量 / 用药 / 复诊提醒
#   - 周期健康报告：按周/月汇总体征、计划与用药
#
# 所有写操作都以 patient_id 行级隔离，患者 ID 只来自运行上下文（JWT → UserContext），
# 工具参数不接受自报 ID，避免越权读写他人数据。

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from langchain_core.tools import tool
from langgraph.prebuilt import ToolRuntime
from loguru import logger
from sqlalchemy import select

from src.core.context import UserContext
from src.tools.runtime import GUEST_HINT, resolve_patient_id

METRIC_TYPES = {
    "blood_pressure": "血压",
    "blood_glucose": "血糖",
    "weight": "体重",
    "heart_rate": "心率",
}

# 周/月报告的回溯窗口
PERIOD_DAYS = {"week": 7, "month": 30}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _fmt(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m-%d %H:%M") if dt else "-"


@tool
async def record_vital_metric(
    metric_type: str,
    value_primary: float,
    runtime: ToolRuntime[UserContext],
    value_secondary: float | None = None,
    period: str = "",
    note: str = "",
    measured_at: str = "",
) -> str:
    """
    记录一次体征测量值：血压 / 血糖 / 体重 / 心率。

    使用时机：
    - "帮我记一下今天血压 130/85"、"刚才测血糖 7.2"、"我体重 65 公斤了"。
    血压必须同时给收缩压和舒张压（value_primary=收缩压，value_secondary=舒张压）。

    Args:
        metric_type : blood_pressure / blood_glucose / weight / heart_rate
        value_primary  : 主值（血压为收缩压）
        value_secondary: 次值（血压为舒张压，其它类型留空）
        period      : 血糖专用，fasting=空腹 / postprandial=餐后 / random=随机
        note        : 备注
        measured_at : 测量时间（ISO 或 "YYYY-MM-DD HH:MM"），留空为当前时间
    """
    patient_id = resolve_patient_id(runtime)
    if patient_id is None:
        return GUEST_HINT

    mtype = (metric_type or "").strip()
    if mtype not in METRIC_TYPES:
        return f"暂不支持的指标类型：{metric_type}。可用：{'、'.join(METRIC_TYPES)}。"
    if mtype == "blood_pressure" and value_secondary is None:
        return "记录血压需要同时提供收缩压和舒张压（例如 130/85）。"

    from src.infra.database import AsyncSessionLocal
    from src.modules.patient.model import VitalMetric
    from src.modules.patient.service import LEVEL_LABELS, METRIC_META, evaluate_metric, parse_datetime

    level = evaluate_metric(mtype, value_primary, value_secondary, period or None)
    measured = parse_datetime(measured_at) or _now()

    try:
        async with AsyncSessionLocal() as db:
            metric = VitalMetric(
                patient_id=patient_id,
                metric_type=mtype,
                value_primary=float(value_primary),
                value_secondary=float(value_secondary) if value_secondary is not None else None,
                unit=METRIC_META.get(mtype, {}).get("unit", ""),
                period=period.strip() or None,
                level=level,
                source="manual",
                note=note.strip() or None,
                measured_at=measured,
            )
            db.add(metric)
            await db.commit()
            await db.refresh(metric)
    except Exception as e:
        logger.warning(f"体征记录失败: {type(e).__name__}: {e}")
        return "体征记录服务暂时不可用，请稍后再试。"

    meta = METRIC_META.get(mtype, {})
    value_text = f"{value_primary:g}"
    if value_secondary is not None:
        value_text += f"/{value_secondary:g}"
    period_text = f"（{period}）" if period else ""
    return (
        f"已记录{METRIC_TYPES[mtype]}：{value_text}{meta.get('unit', '')}{period_text}，"
        f"评估为「{LEVEL_LABELS.get(level, level)}」，时间 {_fmt(metric.measured_at)}。"
        f"{'（数值偏高/偏低，建议连续监测并在复诊时告知医生。）' if level != 'normal' else ''}"
    )


@tool
async def get_metric_trends(
    runtime: ToolRuntime[UserContext],
    metric_type: str = "",
    days: int = 30,
) -> str:
    """
    查看体征趋势并生成趋势图（时序数据 + 文本折线图 + 均值/极值/异常次数）。

    使用时机：
    - "看看我最近的血压趋势"、"我这一个月血糖怎么样"、"给我画个体重变化图"。

    Args:
        metric_type: 留空返回全部指标；可指定 blood_pressure / blood_glucose / weight / heart_rate
        days       : 回溯天数，默认 30
    """
    patient_id = resolve_patient_id(runtime)
    if patient_id is None:
        return GUEST_HINT

    days = max(1, min(int(days or 30), 365))
    mtype = (metric_type or "").strip()
    if mtype and mtype not in METRIC_TYPES:
        return f"暂不支持的指标类型：{metric_type}。可用：{'、'.join(METRIC_TYPES)}。"

    from src.infra.database import AsyncSessionLocal
    from src.modules.patient.model import VitalMetric
    from src.modules.patient.service import METRIC_META, build_trend_chart, metric_label

    since = _now() - timedelta(days=days)
    try:
        async with AsyncSessionLocal() as db:
            q = (
                select(VitalMetric)
                .where(VitalMetric.patient_id == patient_id, VitalMetric.measured_at >= since)
                .order_by(VitalMetric.measured_at.asc())
            )
            if mtype:
                q = q.where(VitalMetric.metric_type == mtype)
            rows = (await db.execute(q)).scalars().all()
    except Exception as e:
        logger.warning(f"体征趋势查询失败: {type(e).__name__}: {e}")
        return "趋势查询服务暂时不可用，请稍后再试。"

    if not rows:
        return f"最近 {days} 天没有{'『' + METRIC_TYPES.get(mtype, '') + '』' if mtype else ''}测量记录，先记录几次才能看趋势。"

    series: dict[str, list[dict]] = {}
    for m in rows:
        series.setdefault(m.metric_type, []).append(
            {
                "t": _fmt(m.measured_at),
                "primary": m.value_primary,
                "secondary": m.value_secondary,
                "level": m.level,
            }
        )

    parts = [f"【最近 {days} 天体征趋势】"]
    for key, points in series.items():
        unit = METRIC_META.get(key, {}).get("unit", "")
        abnormal = sum(1 for p in points if p["level"] != "normal")
        parts.append(f"\n{metric_label(key)}（{len(points)} 次，异常 {abnormal} 次）")
        parts.append(build_trend_chart(points, unit=unit))
        latest = points[-1]
        latest_text = f"{latest['primary']:g}"
        if latest.get("secondary") is not None:
            latest_text += f"/{latest['secondary']:g}"
        parts.append(f"最近一次：{latest_text}{unit}（{latest['t']}）")
    parts.append("\n趋势图文本可在支持 Markdown 的客户端里直接阅读；需要更细的图可在「健康」页面查看。")
    return "\n".join(parts)


@tool
async def manage_health_plan(
    action: str,
    runtime: ToolRuntime[UserContext],
    title: str = "",
    plan_type: str = "other",
    target_text: str = "",
    frequency: str = "",
    plan_id: int = 0,
    note: str = "",
) -> str:
    """
    管理健康计划：建立、查看、完成/放弃。

    使用时机：
    - "我要定个计划三个月减 5 公斤"、"每天走 6000 步"、"我的计划完成了"。

    Args:
        action     : add=新增 / list=查看进行中 / done=完成 / drop=放弃 / all=全部
        title      : 计划标题（add 必填），如"每日快走 30 分钟"
        plan_type  : diet=饮食 / exercise=运动 / medication=用药 / monitor=监测 / other=其它
        target_text: 目标描述，如"3 个月减重 5kg"
        frequency  : 执行频次，如"每日"
        plan_id    : done/drop 时的计划 ID
        note       : 备注
    """
    patient_id = resolve_patient_id(runtime)
    if patient_id is None:
        return GUEST_HINT

    act = (action or "list").strip().lower()
    from src.infra.database import AsyncSessionLocal
    from src.modules.patient.model import HealthPlan

    try:
        async with AsyncSessionLocal() as db:
            if act == "add":
                if not title.strip():
                    return "建立健康计划需要给出计划标题。"
                plan = HealthPlan(
                    patient_id=patient_id,
                    title=title.strip(),
                    plan_type=plan_type.strip() or "other",
                    target_text=target_text.strip() or None,
                    frequency=frequency.strip() or None,
                    start_date=date.today(),
                    status="active",
                    note=note.strip() or None,
                )
                db.add(plan)
                await db.commit()
                await db.refresh(plan)
                return f"已建立健康计划：{plan.title}（ID {plan.id}）" + (
                    f"，目标：{plan.target_text}" if plan.target_text else ""
                )

            if act in {"done", "drop"}:
                plan = await db.get(HealthPlan, plan_id)
                if plan is None or plan.patient_id != patient_id:
                    return f"未找到 ID 为 {plan_id} 的健康计划，请先用 action=list 确认。"
                plan.status = "completed" if act == "done" else "abandoned"
                plan.end_date = date.today()
                await db.commit()
                return f"计划「{plan.title}」已标记为{'已完成' if act == 'done' else '已放弃'}。"

            q = select(HealthPlan).where(HealthPlan.patient_id == patient_id)
            if act != "all":
                q = q.where(HealthPlan.status == "active")
            rows = (await db.execute(q.order_by(HealthPlan.id.desc()).limit(20))).scalars().all()
            if not rows:
                return "暂无进行中的健康计划。可以和用户一起定一个小目标（如每日 6000 步、每日限盐 5g）。"
            lines = ["【健康计划】"]
            for p in rows:
                seg = f"- ID {p.id}｜{p.title}｜{p.status}"
                if p.target_text:
                    seg += f"｜目标 {p.target_text}"
                if p.frequency:
                    seg += f"｜{p.frequency}"
                lines.append(seg)
            return "\n".join(lines)
    except Exception as e:
        logger.warning(f"健康计划操作失败 action={act}: {type(e).__name__}: {e}")
        return "健康计划服务暂时不可用，请稍后再试。"


@tool
async def manage_reminder(
    action: str,
    runtime: ToolRuntime[UserContext],
    title: str = "",
    remind_time: str = "08:00",
    frequency: str = "daily",
    weekdays: str = "",
    reminder_id: int = 0,
    note: str = "",
) -> str:
    """
    创建、查看、停用健康提醒（测量 / 用药 / 复诊）。

    使用时机：
    - "每天早上提醒我量血压"、"提醒我晚上 8 点吃药"、"我有哪些提醒"。

    Args:
        action     : add=新增 / list=查看 / off=停用 / on=启用
        title      : 提醒标题（add 必填），如"测量血压"
        remind_time: 提醒时间 HH:MM，默认 08:00
        frequency  : daily=每天 / weekly=每周
        weekdays   : weekly 时的星期，逗号分隔 1-7（1=周一）
        reminder_id: off/on 时的提醒 ID
        note       : 备注
    """
    patient_id = resolve_patient_id(runtime)
    if patient_id is None:
        return GUEST_HINT

    act = (action or "list").strip().lower()
    from src.infra.database import AsyncSessionLocal
    from src.modules.patient.model import HealthReminder

    try:
        async with AsyncSessionLocal() as db:
            if act == "add":
                if not title.strip():
                    return "创建提醒需要给出提醒标题。"
                reminder = HealthReminder(
                    patient_id=patient_id,
                    title=title.strip(),
                    remind_time=remind_time.strip() or "08:00",
                    frequency="weekly" if frequency == "weekly" else "daily",
                    weekdays=weekdays.strip() or None,
                    enabled=True,
                    note=note.strip() or None,
                )
                db.add(reminder)
                await db.commit()
                await db.refresh(reminder)
                freq_text = "每周" + (f" {reminder.weekdays}" if reminder.weekdays else "") \
                    if reminder.frequency == "weekly" else "每天"
                return f"已创建提醒：{reminder.title}（{freq_text} {reminder.remind_time}，ID {reminder.id}）。"

            if act in {"off", "on"}:
                reminder = await db.get(HealthReminder, reminder_id)
                if reminder is None or reminder.patient_id != patient_id:
                    return f"未找到 ID 为 {reminder_id} 的提醒，请先用 action=list 确认。"
                reminder.enabled = act == "on"
                await db.commit()
                return f"提醒「{reminder.title}」已{'启用' if reminder.enabled else '停用'}。"

            rows = (
                await db.execute(
                    select(HealthReminder)
                    .where(HealthReminder.patient_id == patient_id)
                    .order_by(HealthReminder.id.desc())
                    .limit(20)
                )
            ).scalars().all()
            if not rows:
                return "暂无健康提醒。可以建议用户设置每日测量血压/血糖或用药提醒。"
            lines = ["【健康提醒】"]
            for r in rows:
                freq_text = f"每周 {r.weekdays}" if r.frequency == "weekly" else "每天"
                lines.append(
                    f"- ID {r.id}｜{r.title}｜{freq_text} {r.remind_time}｜{'启用' if r.enabled else '停用'}"
                )
            return "\n".join(lines)
    except Exception as e:
        logger.warning(f"健康提醒操作失败 action={act}: {type(e).__name__}: {e}")
        return "健康提醒服务暂时不可用，请稍后再试。"


@tool
async def generate_period_report(
    runtime: ToolRuntime[UserContext],
    period_type: str = "week",
) -> str:
    """
    生成周期健康报告：汇总体征趋势、健康计划与用药记录，落库后可重复查看。

    使用时机：
    - "给我出一份本周健康报告"、"这个月的健康总结"。

    Args:
        period_type: week=近 7 天 / month=近 30 天
    """
    patient_id = resolve_patient_id(runtime)
    if patient_id is None:
        return GUEST_HINT

    ptype = (period_type or "week").strip().lower()
    if ptype not in PERIOD_DAYS:
        return "周期只支持 week（近 7 天）或 month（近 30 天）。"

    from src.infra.database import AsyncSessionLocal
    from src.modules.patient.model import HealthPlan, HealthPeriodReport, MedicationRecord, VitalMetric
    from src.modules.patient.service import METRIC_META, build_period_report

    end = date.today()
    start = end - timedelta(days=PERIOD_DAYS[ptype] - 1)
    since = _now() - timedelta(days=PERIOD_DAYS[ptype])

    try:
        async with AsyncSessionLocal() as db:
            metrics = (
                await db.execute(
                    select(VitalMetric)
                    .where(VitalMetric.patient_id == patient_id, VitalMetric.measured_at >= since)
                    .order_by(VitalMetric.measured_at.asc())
                )
            ).scalars().all()
            plans = (
                await db.execute(
                    select(HealthPlan).where(HealthPlan.patient_id == patient_id).limit(20)
                )
            ).scalars().all()
            meds = (
                await db.execute(
                    select(MedicationRecord)
                    .where(MedicationRecord.patient_id == patient_id)
                    .order_by(MedicationRecord.id.desc())
                    .limit(20)
                )
            ).scalars().all()

            metric_dicts = [
                {
                    "metric_type": m.metric_type,
                    "primary": m.value_primary,
                    "secondary": m.value_secondary,
                    "unit": m.unit or METRIC_META.get(m.metric_type, {}).get("unit", ""),
                    "level": m.level,
                }
                for m in metrics
            ]
            plan_dicts = [
                {"title": p.title, "status": p.status, "target_text": p.target_text} for p in plans
            ]
            med_dicts = [
                {"drug_name": m.drug_name, "dosage": m.dosage, "frequency": m.frequency, "status": m.status}
                for m in meds
            ]

            summary, content, stats = build_period_report(
                ptype, start, end, metric_dicts, plan_dicts, med_dicts
            )
            report = HealthPeriodReport(
                patient_id=patient_id,
                period_type=ptype,
                period_start=start,
                period_end=end,
                summary=summary,
                content=content,
                stats=stats,
            )
            db.add(report)
            await db.commit()
    except Exception as e:
        logger.warning(f"周期健康报告生成失败: {type(e).__name__}: {e}")
        return "周期健康报告服务暂时不可用，请稍后再试。"

    return f"{summary}\n\n{content}"
