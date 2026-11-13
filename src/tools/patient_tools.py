# src/tools/patient_tools.py
#
# 患者身份相关工具：供 Supervisor 回答"我是谁 / 我的资料"类问题。
#
# 职责边界（与需求一致）：
#   - 长期记忆（Milvus store）由 search_memory 负责，优先使用；
#   - search_memory 没有可用身份信息时，再由本工具从数据库查询
#     **当前登录患者** 的档案作为兜底。
#   - 未登录（游客）不查库，直接提示登录 —— 避免泄露他人档案。

from __future__ import annotations

from langchain_core.tools import tool
from langgraph.prebuilt import ToolRuntime
from loguru import logger
from sqlalchemy import select

from src.core.context import UserContext


def _resolve_user_id(runtime: ToolRuntime) -> str:
    """优先取 UserContext，回退从 thread_id 解析（与 store_tools 一致）。"""
    ctx = runtime.context
    if ctx is not None and getattr(ctx, "user_id", None):
        return str(ctx.user_id)
    configurable = runtime.config.get("configurable", {}) if runtime.config else {}
    thread_id = configurable.get("thread_id", "") or ""
    return thread_id.split(":")[0] if thread_id else ""


async def _load_patient(patient_id: int) -> dict | None:
    from src.infra.database import AsyncSessionLocal
    from src.modules.medical.model import Patient
    from src.utils.masking import mask_id_card, mask_phone

    async with AsyncSessionLocal() as db:
        row = (await db.execute(select(Patient).where(Patient.id == patient_id))).scalar_one_or_none()
        if row is None:
            return None
        return {
            "name": row.name,
            "gender": row.gender or "未填写",
            "age": f"{row.age} 岁" if row.age else "未填写",
            "blood_type": row.blood_type or "未填写",
            "allergy_history": row.allergy_history or "无记录",
            "medical_history": row.medical_history or "无记录",
            "phone": mask_phone(row.phone) if row.phone else "未填写",
            "id_card": mask_id_card(row.id_card) if row.id_card else "未填写",
        }


@tool
async def get_my_profile(runtime: ToolRuntime[UserContext]) -> str:
    """
    查询当前登录患者的个人档案（姓名、性别、年龄、血型、过敏史、既往病史、脱敏联系方式）。

    使用时机：
    - 用户问"我是谁"、"我的资料/我的病历/我对什么药过敏"等与自身身份相关的问题；
    - 且 search_memory 未检索到可用的身份信息（或没有历史记录）时，
      用本工具从数据库兜底查询当前登录患者的档案。

    未登录（游客）调用会返回"未登录"提示，此时如实告知用户登录后可查询即可，禁止编造。
    """
    user_id = _resolve_user_id(runtime)
    if not user_id or user_id == "anon" or not user_id.isdigit():
        logger.info(f"get_my_profile 游客模式，拒绝查询档案 user_id={user_id!r}")
        return (
            "当前为未登录（游客）状态，无法查询个人档案。"
            "请告知用户登录后可查看姓名、过敏史、既往病史等档案信息；本次对话内容也不会被保存。"
        )

    try:
        profile = await _load_patient(int(user_id))
    except Exception as e:
        logger.warning(f"患者档案查询失败: {type(e).__name__}: {e}")
        return "患者档案暂时查询不到（服务繁忙），请稍后再问，或前往“我的”页面查看。"

    if profile is None:
        return "未查询到您的患者档案。可以请用户前往“我的”页面完善档案，或先告知称呼。"

    return (
        "【患者档案（数据库兜底查询）】\n"
        f"- 姓名：{profile['name']}\n"
        f"- 性别：{profile['gender']}　年龄：{profile['age']}\n"
        f"- 血型：{profile['blood_type']}\n"
        f"- 过敏史：{profile['allergy_history']}\n"
        f"- 既往病史：{profile['medical_history']}\n"
        f"- 联系电话：{profile['phone']}\n"
        f"- 身份证号：{profile['id_card']}"
    )


async def _load_recent_reports(patient_id: int, limit: int) -> list[dict]:
    """取最近 N 份报告及其结构化指标（只读结构化字段，不读报告原文）。"""
    from src.infra.database import AsyncSessionLocal
    from src.modules.patient.model import HealthReport, ReportMetric

    async with AsyncSessionLocal() as db:
        reports = (
            await db.execute(
                select(HealthReport)
                .where(HealthReport.patient_id == patient_id)
                .order_by(HealthReport.created_at.desc())
                .limit(limit)
            )
        ).scalars().all()
        if not reports:
            return []

        ids = [r.id for r in reports]
        metrics = (
            await db.execute(
                select(ReportMetric)
                .where(ReportMetric.report_id.in_(ids))
                .order_by(ReportMetric.report_id.desc(), ReportMetric.sort_order.asc())
            )
        ).scalars().all()

    grouped: dict[int, list] = {rid: [] for rid in ids}
    for m in metrics:
        grouped.setdefault(m.report_id, []).append(m)

    out = []
    for r in reports:
        out.append({
            "id": r.id,
            "file_name": r.file_name,
            "report_type": r.report_type,
            "report_date": str(r.report_date) if r.report_date else "",
            "hospital": r.hospital or "",
            "status": r.status,
            "summary": r.summary or "",
            "abnormal_count": r.abnormal_count,
            "metrics": grouped.get(r.id, []),
        })
    return out


@tool
async def get_recent_reports(runtime: ToolRuntime[UserContext], limit: int = 3) -> str:
    """
    查询当前登录患者最近上传的检查报告及其结构化指标（含异常项与参考区间）。

    使用时机：
    - 用户说"帮我看看我的报告""我上次的血常规怎么样""对比一下我的检查结果"；
    - 需要结合历史报告判断某项指标趋势时。

    报告原文只存在对象存储，这里只返回结构化指标；没有报告时会返回空提示，
    此时如实告知用户先上传报告，禁止编造指标数值。
    """
    user_id = _resolve_user_id(runtime)
    if not user_id or user_id == "anon" or not user_id.isdigit():
        return "当前为未登录（游客）状态，无法读取历史报告。请提示用户登录后查看。"

    limit = max(1, min(int(limit or 3), 5))
    try:
        reports = await _load_recent_reports(int(user_id), limit)
    except Exception as e:
        logger.warning(f"历史报告查询失败: {type(e).__name__}: {e}")
        return "暂时读取不到历史报告（服务繁忙），请稍后再试。"

    if not reports:
        return "该患者还没有上传过检查报告。请提示用户先在「报告中心」上传报告后再来解读。"

    parts = ["【最近上传的报告（结构化指标）】"]
    for idx, r in enumerate(reports, 1):
        parts.append(
            f"\n{idx}. {r['file_name']}｜类型 {r['report_type']}｜日期 {r['report_date'] or '未识别'}"
            f"｜机构 {r['hospital'] or '未填写'}｜状态 {r['status']}"
        )
        if r["summary"]:
            parts.append(f"   摘要：{r['summary']}")
        metrics = r["metrics"]
        if not metrics:
            parts.append("   指标：该报告尚未解析出结构化指标（可提示用户重新上传或录入数值）")
            continue
        abnormal = [m for m in metrics if (m.abnormal or "normal") != "normal"]
        if abnormal:
            parts.append(f"   异常指标（{len(abnormal)}/{len(metrics)}）：")
            for m in abnormal[:8]:
                ref = m.ref_text or (
                    f"{m.ref_low:g}-{m.ref_high:g}" if m.ref_low is not None and m.ref_high is not None else "未标注"
                )
                parts.append(f"   - {m.name} {m.value_text}{m.unit}（参考 {ref}，{m.abnormal}）")
        else:
            parts.append(f"   指标：共 {len(metrics)} 项，均在参考区间内")
    return "\n".join(parts)
