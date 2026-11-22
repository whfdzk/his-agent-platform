# src/tools/medication_tools.py
#
# MedicationAgent（药品知识服务）的领域工具：
#   - 药品说明书 / 成分 / 禁忌 / 不良反应：结构化说明书（PG）优先，向量库兜底
#   - 过敏风险：患者过敏史 × 药品成分/禁忌 的关键词关联提示
#   - 用药记录：患者自己的用药台账增删改查
#
# 数据来源约定：只用已入库的审核版说明书（drug_details）与知识库文档；
# 查不到时如实说"暂未覆盖"，禁止凭记忆编造成分、禁忌与不良反应。

from __future__ import annotations

import re
from datetime import date

from langchain_core.tools import tool
from langgraph.prebuilt import ToolRuntime
from loguru import logger
from sqlalchemy import or_, select

from src.core.context import UserContext
from src.core.concurrency import to_thread
from src.tools.runtime import GUEST_HINT, resolve_patient_id

# 说明书章节 → (展示名, DrugDetail 字段)
SECTION_FIELDS: dict[str, tuple[str, str]] = {
    "ingredient": ("主要成分", "ingredient"),
    "indication": ("适应症", "indication"),
    "dosage": ("用法用量", "usage_dosage"),
    "adverse": ("不良反应", "adverse_reaction"),
    "contraindication": ("禁忌", "contraindication"),
    "precaution": ("注意事项", "precaution"),
    "interaction": ("药物相互作用", "interaction"),
    "storage": ("储存条件", "storage"),
}

# 从说明书全文里兜底抽取成分（结构化字段为空时）：匹配"成分/主要成分"开头的段落
_INGREDIENT_RE = re.compile(r"(?:【)?(?:主要)?成分(?:】)?[:：]\s*([^\n。；]{2,200})")
_MAX_TEXT = 600


async def _load_drug(drug_name: str) -> dict | None:
    """按药品名/别名查结构化说明书；返回 None 表示库里没有。"""
    from src.infra.database import AsyncSessionLocal
    from src.modules.medical.model import Drug, DrugDetail

    keyword = f"%{drug_name.strip()}%"
    async with AsyncSessionLocal() as db:
        row = (
            await db.execute(
                select(Drug, DrugDetail)
                .outerjoin(DrugDetail, DrugDetail.drug_id == Drug.id)
                .where(or_(Drug.name.like(keyword), Drug.alias.like(keyword)))
                .order_by(Drug.id.asc())
                .limit(1)
            )
        ).first()
        if row is None:
            return None
        drug, detail = row
        return {
            "name": drug.name,
            "alias": drug.alias or "",
            "category": drug.category or "",
            "manufacturer": drug.manufacturer or "",
            "approval_number": drug.approval_number or "",
            "is_otc": drug.is_otc,
            "ingredient": detail.ingredient if detail else None,
            "indication": detail.indication if detail else None,
            "usage_dosage": detail.usage_dosage if detail else None,
            "adverse_reaction": detail.adverse_reaction if detail else None,
            "contraindication": detail.contraindication if detail else None,
            "precaution": detail.precaution if detail else None,
            "interaction": detail.interaction if detail else None,
            "storage": detail.storage if detail else None,
            "full_instruction": detail.full_instruction if detail else None,
        }


def _clip(text: str | None) -> str:
    if not text:
        return ""
    return text if len(text) <= _MAX_TEXT else text[:_MAX_TEXT] + "…"


async def _search_instruction_docs(drug_name: str, section: str) -> str:
    """结构化说明书缺失时，从知识库（Milvus 药品说明书）兜底检索。"""
    from src.core.config import get_llm
    from src.infra.embedding import get_embeddings
    from src.infra.milvus_client import get_milvus_client
    from src.knowledge.doc_rag import search_docs

    question = f"{drug_name}的{SECTION_FIELDS.get(section, ('说明书', ''))[0]}"
    try:
        # 同步建连放进线程池，避免阻塞事件循环
        milvus_client = await to_thread(get_milvus_client)
        return await search_docs(
            question=question,
            embedding_model=get_embeddings(),
            milvus_client=milvus_client,
            llm=get_llm(temperature=0.1),
            doc_type="drug_instruction",
            role="patient",
            with_grounding=True,
        )
    except Exception as e:
        logger.warning(f"药品说明书向量检索失败: {type(e).__name__}: {e}")
        return ""


@tool
async def query_drug_info(drug_name: str, section: str = "") -> str:
    """
    查询药品说明书内容：主要成分、适应症、用法用量、不良反应、禁忌、注意事项、药物相互作用、储存条件。

    使用时机：
    - 用户问"某某药是治什么的/怎么吃/有什么副作用/什么人不能吃/成分是什么"；
    - 需要回答药品成分、禁忌与不良反应、相互作用等说明书类问题。

    Args:
        drug_name: 药品名称（通用名或商品名）
        section : 可选章节，取值 ingredient/indication/dosage/adverse/contraindication/precaution/interaction/storage；留空返回全部
    """
    name = (drug_name or "").strip()
    if not name:
        return "请提供药品名称后再查询。"

    try:
        drug = await _load_drug(name)
    except Exception as e:
        logger.warning(f"药品说明书查询失败: {type(e).__name__}: {e}")
        drug = None

    if drug is None:
        # 结构化库没有：退到知识库文档，仍然查不到就如实说明
        fallback = await _search_instruction_docs(name, section)
        if fallback:
            return f"【知识库检索结果】\n{fallback}"
        return (
            f"药品库与知识库中暂未收录「{name}」的说明书，无法确认其成分/禁忌/不良反应。"
            "请提示用户以药品包装说明书为准，或咨询药师；不要凭记忆编造。"
        )

    sections = [section] if section in SECTION_FIELDS else list(SECTION_FIELDS)
    parts = [f"【{drug['name']} 说明书（结构化数据）】"]
    if drug["alias"]:
        parts.append(f"别名/商品名：{drug['alias']}")
    if drug["category"]:
        parts.append(f"分类：{drug['category']}")
    if drug["manufacturer"]:
        parts.append(f"生产厂家：{drug['manufacturer']}")
    if drug["approval_number"]:
        parts.append(f"批准文号：{drug['approval_number']}")
    parts.append(f"是否 OTC：{'是' if drug['is_otc'] else '否'}")

    for key in sections:
        label, field = SECTION_FIELDS[key]
        value = drug.get(field)
        if not value and field == "ingredient" and drug.get("full_instruction"):
            # 成分字段缺失：从说明书原文抽一次，抽不到就留空
            matched = _INGREDIENT_RE.search(drug["full_instruction"])
            value = matched.group(1) if matched else None
        parts.append(f"{label}：{_clip(value) or '（说明书未收录）'}")

    parts.append("说明：以上为已入库说明书摘要，具体用药请遵医嘱并核对药品包装说明书。")
    return "\n".join(parts)


@tool
async def check_drug_allergy_risk(drug_name: str, runtime: ToolRuntime[UserContext]) -> str:
    """
    过敏风险关联提示：把患者的过敏史与药品成分、禁忌做关键词比对，给出风险提示。

    使用时机：
    - 用户问"我对青霉素/头孢/某药过敏，能吃这个药吗"；
    - 开药或新增用药记录前需要核对过敏史。

    Args:
        drug_name: 待核查的药品名称
    """
    patient_id = resolve_patient_id(runtime)
    if patient_id is None:
        return GUEST_HINT + "（过敏史需要登录后才能核对，请同时提醒用户主动说明过敏史。）"

    name = (drug_name or "").strip()
    if not name:
        return "请提供药品名称后再核查过敏风险。"

    from src.infra.database import AsyncSessionLocal
    from src.modules.medical.model import Patient

    try:
        async with AsyncSessionLocal() as db:
            allergy = (
                await db.execute(select(Patient.allergy_history).where(Patient.id == patient_id))
            ).scalar_one_or_none()
    except Exception as e:
        logger.warning(f"过敏史读取失败: {type(e).__name__}: {e}")
        return "暂时读取不到您的过敏史（服务繁忙），请稍后再试；如已知过敏请务必主动告知医生。"

    if not allergy or not allergy.strip():
        return (
            "档案中暂无过敏史记录，无法自动比对。\n"
            "请提示用户：如已知对某类药物过敏，务必在用药前告知医生或药师；"
            "也可以在「我的-健康档案」补充过敏史，后续会自动关联提示。"
        )

    drug = await _load_drug(name)
    if drug is None:
        return (
            f"药品库暂未收录「{name}」，无法做成分比对。\n"
            f"已知过敏史：{allergy}。请提示用户核对药品包装成分表，或咨询药师。"
        )

    # 过敏原可能写成"青霉素""头孢""海鲜"等；成分/禁忌文本里命中任一关键词即提示
    allergens = [a.strip() for a in re.split(r"[,，、;；\s]+", allergy) if len(a.strip()) >= 2]
    haystack = " ".join(
        str(drug.get(f) or "")
        for f in ("name", "alias", "ingredient", "contraindication", "precaution", "indication")
    )
    hits = [a for a in allergens if a in haystack]

    lines = [f"【过敏风险核查】药品：{drug['name']}｜患者过敏史：{allergy}"]
    if hits:
        lines.append(f"⚠️ 命中关联：{'、'.join(hits)}。该药品的成分/禁忌提示中包含您过敏的物质。")
        lines.append(f"相关原文：{_clip(drug.get('ingredient') or drug.get('contraindication') or '')}")
        lines.append("建议：不要自行使用，请咨询医生/药师确认替代方案；如已出现皮疹、呼吸困难等反应请立即就医。")
    else:
        lines.append("未在药品成分/禁忌文本中匹配到已记录的过敏原（不等于绝对安全）。")
        if drug.get("ingredient"):
            lines.append(f"该药主要成分：{_clip(drug['ingredient'])}")
        lines.append("建议：首次使用仍应观察是否出现异常反应；孕妇、儿童、肝肾功能不全者请先咨询医生。")
    return "\n".join(lines)


@tool
async def manage_medication_record(
    action: str,
    runtime: ToolRuntime[UserContext],
    drug_name: str = "",
    dosage: str = "",
    frequency: str = "",
    indication: str = "",
    record_id: int = 0,
    note: str = "",
) -> str:
    """
    管理患者的用药记录台账（新增/查看/停用）。

    使用时机：
    - "帮我记一下我在吃二甲双胍"、"我最近在吃什么药"、"这个药我停了"；
    - 开药后需要把用药写进台账，便于后续相互作用与过敏核查。

    Args:
        action   : add=新增 / list=查看在用 / stop=停用 / history=全部历史
        drug_name: 药品名称（add 必填）
        dosage   : 单次剂量，如 "1 片"、"0.5g"
        frequency: 频次，如 "每日两次"
        indication: 用药目的/适应症
        record_id: stop 时的记录 ID
        note     : 备注
    """
    patient_id = resolve_patient_id(runtime)
    if patient_id is None:
        return GUEST_HINT

    act = (action or "list").strip().lower()
    from src.infra.database import AsyncSessionLocal
    from src.modules.patient.model import MedicationRecord

    try:
        async with AsyncSessionLocal() as db:
            if act == "add":
                if not drug_name.strip():
                    return "新增用药记录需要提供药品名称。"
                record = MedicationRecord(
                    patient_id=patient_id,
                    drug_name=drug_name.strip(),
                    dosage=dosage.strip() or None,
                    frequency=frequency.strip() or None,
                    indication=indication.strip() or None,
                    start_date=date.today(),
                    status="active",
                    source="manual",
                    note=note.strip() or None,
                )
                db.add(record)
                await db.commit()
                await db.refresh(record)
                return (
                    f"已记录用药：{record.drug_name}"
                    f"{('，' + record.dosage) if record.dosage else ''}"
                    f"{('，' + record.frequency) if record.frequency else ''}"
                    f"（记录 ID {record.id}）。后续开药会自动纳入相互作用与过敏核查。"
                )

            if act == "stop":
                record = await db.get(MedicationRecord, record_id)
                if record is None or record.patient_id != patient_id:
                    return f"未找到 ID 为 {record_id} 的用药记录，请先用 action=list 确认记录 ID。"
                record.status = "stopped"
                record.end_date = date.today()
                await db.commit()
                return f"已停用「{record.drug_name}」的用药记录。"

            statuses = ["active"] if act == "list" else None
            q = select(MedicationRecord).where(MedicationRecord.patient_id == patient_id)
            if statuses:
                q = q.where(MedicationRecord.status.in_(statuses))
            rows = (
                await db.execute(q.order_by(MedicationRecord.id.desc()).limit(20))
            ).scalars().all()

            if not rows:
                return "暂无用药记录。可让用户报出正在吃的药，帮其建立用药台账。"
            lines = ["【用药记录】"]
            for r in rows:
                seg = f"- ID {r.id}｜{r.drug_name}"
                if r.dosage:
                    seg += f"｜{r.dosage}"
                if r.frequency:
                    seg += f"｜{r.frequency}"
                if r.indication:
                    seg += f"｜用于{r.indication}"
                seg += f"｜{r.status}"
                if r.start_date:
                    seg += f"｜起始 {r.start_date}"
                lines.append(seg)
            return "\n".join(lines)
    except Exception as e:
        logger.warning(f"用药记录操作失败 action={act}: {type(e).__name__}: {e}")
        return "用药记录服务暂时不可用，请稍后再试。"
