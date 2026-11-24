# src/modules/clinical/triage.py
#
# 入口急症闸门（B3）。
#
# 为什么不能只靠问诊图里的 node_check_emergency：
#   supervisor 的提示词里写着"识别到胸痛/呼吸困难/意识不清等急症关键词，立即提示
#   就诊，不要等待问诊流程"，所以真实的红旗症状在 supervisor 这一层就被答完了，
#   根本进不到问诊图。结果是：患者在页面上确实看到了"打 120"，但库里一条记录都没有，
#   "识别过多少红旗、分级是否合理"永远无法统计。
#
# 因此在 chat 入口（supervisor 之前）做一次分级：命中关键词才调 LLM 分级，
# 非 green 则落 clinical_events 并直接把急诊指引回给患者。关键词预筛让绝大多数
# 普通消息零额外开销，只有像"胸口很闷"这种才付一次短 prompt 的代价。
#
# 与 node_check_emergency 共用同一份分级提示词与同一套落库逻辑，避免两处口径漂移。

from __future__ import annotations

import json

from loguru import logger

from src.modules.clinical.events import (
    LEVEL_GREEN,
    LEVEL_ORANGE,
    LEVEL_RED,
    normalize_level,
    record_emergency,
)

# ── 关键词预筛 ───────────────────────────────────────────────────────────
# 只用于"要不要付一次 LLM 分级"，不是判定结果：命中不等于急症，还要分级确认。
TRIGGERS = (
    "胸痛", "胸口", "胸闷", "心口", "心脏", "心跳", "心慌", "心悸",
    "喘不上", "呼吸困难", "憋气", "上不来气", "窒息", "气促",
    "昏迷", "意识不清", "意识模糊", "晕倒", "昏倒", "抽搐", "休克",
    "大出血", "出血不止", "吐血", "便血", "血便",
    "偏瘫", "半身不遂", "失语", "说不出话", "口角歪",
    # 卒中口语表达：患者很少说"偏瘫/失语"，多是"说话不清楚、半边身子使不上劲"
    "说话不清", "说话不利索", "说不清楚话", "吐字不清", "大舌头",
    "半边身子", "半身麻木", "一侧肢体", "肢体无力", "手脚不听", "手脚不灵",
    "胳膊抬不起", "腿抬不起", "走路跑偏", "嘴歪", "口角歪斜",
    "剧烈头痛", "头痛欲裂", "喉咙紧", "喉头", "过敏",
    "剧痛", "疼得受不了", "快不行", "濒死",
)

# LLM 分级不可用时，这些词单独出现也要按 red 提示（宁可误报，不可漏报）
STRONG_TRIGGERS = (
    "胸痛", "呼吸困难", "喘不上", "窒息", "昏迷", "意识不清", "抽搐",
    "休克", "大出血", "偏瘫", "失语", "快不行", "濒死",
    # 卒中：4.5 小时溶栓时间窗，分级模型不可用时也必须按 red 提示
    "说话不清", "半边身子", "一侧肢体", "口角歪斜", "嘴歪", "肢体无力",
)


def hits(text: str) -> bool:
    if not text:
        return False
    return any(k in text for k in TRIGGERS)


def _has_strong(text: str) -> bool:
    return any(k in text for k in STRONG_TRIGGERS)


# 卒中：4.5 小时溶栓窗，"今天内去急诊"不够，必须立即 120。
# LLM 分级对"说话不清楚+半边身子使不上劲"给过 orange，这里做确定性升级：
# 一个高特异词（偏瘫/嘴歪/半边身子…）+ 一个支持词，或两个高特异词，即判 red。
_STROKE_SPECIFIC = ("偏瘫", "半身不遂", "失语", "口角歪斜", "嘴歪", "半边身子", "一侧肢体")
_STROKE_SUPPORT = ("说话不清", "说不清楚话", "吐字不清", "肢体无力", "手脚不听",
                   "走路跑偏", "胳膊抬不起", "腿抬不起", "嘴歪")


def _stroke_red(text: str) -> bool:
    specific = sum(1 for k in _STROKE_SPECIFIC if k in text)
    support = sum(1 for k in _STROKE_SUPPORT if k in text)
    return (specific >= 1 and support >= 1) or specific >= 2


def emergency_reply(level: str, reason: str = "") -> str:
    """按等级给出就医指引：red 必须 120，orange 今日急诊，yellow 允许门诊。"""
    why = f"原因：{reason}\n\n" if reason else ""
    if level == LEVEL_RED:
        return (
            "⚠️ 根据您描述的情况，这可能是危及生命的紧急情况！\n\n"
            f"{why}"
            "**请立即拨打 120，或马上到最近医院的急诊科就诊，不要自行前往门诊排队。**\n\n"
            "等待救援时请保持安静、不要剧烈活动，不要自行开车；如意识不清请让身边人陪同并保持侧卧位。"
        )
    if level == LEVEL_ORANGE:
        return (
            "⚠️ 您描述的情况需要尽快（今天内）由急诊评估，不建议继续在线等待。\n\n"
            f"{why}"
            "**请立即前往最近医院的急诊科，或拨打 120。**\n\n"
            "如果途中症状加重（意识改变、呼吸困难、疼痛加剧），请直接拨打 120。"
        )
    return (
        "您描述的症状不属于危急情况，但建议今天或明天尽快就医，不要拖到症状加重。\n\n"
        f"{why}"
        "**可以挂相应专科门诊；如果夜间或周末症状明显加重，请直接去急诊科。**\n\n"
        "就诊时把症状开始时间、变化过程和正在服用的药物告诉医生。"
    )


async def classify(llm, text: str) -> dict:
    """调 LLM 做四档分级。解析失败抛异常，由调用方决定兜底。"""
    from src.agents.inquiry.prompts import EMERGENCY_CHECK_PROMPT

    response = await llm.ainvoke(EMERGENCY_CHECK_PROMPT.format(user_input=text))
    content = (response.content or "").strip()
    if "```" in content:
        content = content.split("```")[1].lstrip("json").strip()
    result = json.loads(content)

    raw_level = result.get("level")
    if raw_level is None and result.get("is_emergency"):
        level = LEVEL_RED   # 旧格式兼容：判成急症却没给等级，按最重处理
    else:
        level = normalize_level(raw_level)

    return {
        "level": level,
        "reason": str(result.get("reason") or "").strip(),
        "symptoms": [str(s).strip() for s in (result.get("symptoms") or []) if str(s).strip()],
        "raw_level": raw_level,
    }


async def gate(
    text: str,
    *,
    patient_id: int | None = None,
    user_id: str = "",
    session_id: str = "",
    thread_id: str = "",
    trace_id: str = "",
    persist: bool = True,
    tenant_id: int = 1,
) -> dict | None:
    """入口闸门：返回 None 表示可以继续正常流程；否则返回 {level, reason, reply}。

    - 未命中关键词：直接 None（零额外开销）；
    - 命中后分级为 green：None（正常问诊，不落库）；
    - 非 green：落库（persist 且非 green）并返回急诊指引。
    """
    if not hits(text):
        return None

    level = ""
    reason = ""
    symptoms: list[str] = []
    raw_level = None
    try:
        from src.core.config import get_llm

        verdict = await classify(get_llm(temperature=0.0), text)
        level = verdict["level"]
        reason = verdict["reason"]
        symptoms = verdict["symptoms"]
        raw_level = verdict["raw_level"]
    except Exception as e:  # noqa: BLE001 —— 分级不可用不能挡住就医提示
        logger.warning(f"入口急症分级失败，按关键词兜底: {type(e).__name__}: {e}")
        if not _has_strong(text):
            return None
        level = LEVEL_RED
        reason = "关键词命中急症表现（分级模型不可用，按危急处理）"
        symptoms = [k for k in STRONG_TRIGGERS if k in text]

    if level == LEVEL_GREEN:
        logger.info(f"入口急症闸门：命中关键词但分级为 green（session={session_id}）")
        return None

    if level != LEVEL_RED and _stroke_red(text):
        logger.warning(f"入口急症闸门：卒中表现按危急升级（原判定 {level}，session={session_id}）")
        level = LEVEL_RED
        reason = (reason + "；卒中时间窗，按危急处理") if reason else "卒中表现，按危急处理"

    logger.warning(
        f"入口急症闸门 ⚠️ level={level} session={session_id} reason={reason}"
    )

    if persist:
        await record_emergency(
            level=level,
            reason=reason,
            symptoms=symptoms,
            patient_id=patient_id,
            user_id=user_id,
            session_id=session_id,
            thread_id=thread_id,
            trace_id=trace_id,
            payload={"stage": "chat_entry", "raw_level": raw_level},
            tenant_id=tenant_id,
        )

    return {
        "level": level,
        "reason": reason,
        "reply": emergency_reply(level, reason),
    }
