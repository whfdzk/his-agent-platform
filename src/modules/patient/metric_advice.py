# src/modules/patient/metric_advice.py
#
# 体征记录「风险解读」：单条测量值 + 近期趋势 → 知识库检索（Milvus RAG）→ 流式建议。
#
# 设计约束：
# - 只把数值、年龄段、性别、BMI、慢性病史发给模型；姓名/手机号等可识别信息不出域
# - 知识库不可用（Milvus 挂、未配置 Embedding）时降级为纯模型建议，grounded=False
# - 模型不可用时吐规则化文本，接口不向前端抛异常
# - 输出按【结论】【可能的风险】【值得注意】【何时就医】分段，前端逐段渲染

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from src.core.config import get_llm
from src.modules.patient.service import LEVEL_LABELS, METRIC_META, metric_label

DISCLAIMER = "以上仅为健康管理提示，不能替代医生诊断；如有不适请及时就医。"

# 单条解读的检索预算：超过就先出提示，不让用户干等向量库
RETRIEVAL_TIMEOUT = 20.0
RETRIEVAL_TOP_K = 8
RERANK_TOP_K = 4
# 相关性门槛：低于阈值视为"知识库没覆盖"，宁可让模型说没资料，
# 也不能拿一篇不相关的文档（当前库里就有一份简历）当解读依据。
MIN_RERANK_SCORE = 0.25
MIN_VECTOR_SCORE = 0.35

SYSTEM_PROMPT = """你是平台医疗的健康管理助手，负责解读患者自测的体征数值。

要求：
1. 只依据【参考资料】和通用医学常识回答；资料不足时明确说明"知识库暂未覆盖"，不要编造指南条款
2. 不诊断疾病、不开具处方、不给出具体药物剂量
3. 用患者能看懂的中文，避免堆砌术语
4. 平台分级按下面的参考区间给出，口径差异可一句话带过，不要展开质疑或反复强调"分级不对"
5. 严格按下面四段输出，不要输出其它内容，不要用 Markdown 代码块：

【结论】一句话结论，不超过 40 字
【可能的风险】2-4 条，每条以 "- " 开头
【值得注意】2-4 条，每条以 "- " 开头
【何时就医】1-3 条，每条以 "- " 开头"""


@dataclass(slots=True)
class MetricSnapshot:
    """解读所需的全部字段快照。

    刻意不传 ORM 对象：解读是流式的，请求级 session 必须在开始推送前关闭，
    否则客户端中途断开时连接会在取消作用域里被 terminate（asyncpg 报
    "Exception terminating connection"）。
    """

    metric_id: int | None
    metric_type: str
    value_primary: float
    value_secondary: float | None
    unit: str
    level: str
    period: str | None = None
    gender: str | None = None
    age: int | None = None
    height_cm: float | None = None
    weight_kg: float | None = None
    chronic_diseases: str | None = None
    recent_primary: list[float] = field(default_factory=list)


def _metric_meta(metric_type: str) -> dict:
    return METRIC_META.get(
        metric_type, {"label": metric_type, "unit": "", "primary": "数值", "secondary": ""}
    )


def _num(value: Any) -> str:
    if value is None:
        return "-"
    try:
        return f"{float(value):g}"
    except (TypeError, ValueError):
        return str(value)


def format_value(metric: MetricSnapshot) -> str:
    """血压显示 收缩压/舒张压，其余显示单值 + 单位。"""
    meta = _metric_meta(metric.metric_type)
    unit = metric.unit or meta.get("unit", "")
    if metric.metric_type == "blood_pressure":
        text = f"{_num(metric.value_primary)}/{_num(metric.value_secondary)}"
    else:
        text = _num(metric.value_primary)
    return f"{text} {unit}".strip()


def _period_label(period: str | None) -> str:
    return {"fasting": "空腹", "postprandial": "餐后 2 小时", "random": "随机"}.get(period or "", "")


def _age_band(age: int | None) -> str:
    """年龄段而不是精确年龄：够模型判断风险，又算不上强识别信息。"""
    if not age:
        return "年龄未知"
    if age < 18:
        return "未成年"
    if age < 40:
        return "18-39 岁"
    if age < 60:
        return "40-59 岁"
    return "60 岁及以上"


def _ref_hint(metric_type: str) -> str:
    """与 service.evaluate_metric 的阈值保持一致，避免分级和解读互相打架。"""
    return {
        "blood_pressure": "收缩压 90-139、舒张压 60-89 mmHg（≥140/90 起算偏高）",
        "blood_glucose": "空腹 3.9-6.1 mmol/L（餐后/随机 <11.1）",
        "heart_rate": "55-100 次/分",
        "weight": "单次体重本身不判风险，结合 BMI 18.5-23.9 判断",
    }.get(metric_type, "以检验报告参考区间为准")


def build_question(metric: MetricSnapshot) -> str:
    """组装检索/生成用的问句：数值 + 分级 + 人群特征 + 近期趋势。"""
    label = metric_label(metric.metric_type)
    parts = [
        f"{label} {format_value(metric)}",
        f"分级：{LEVEL_LABELS.get(metric.level, metric.level)}"
        f"（通用成人参考区间：{_ref_hint(metric.metric_type)}）",
    ]

    period = _period_label(metric.period)
    if period:
        parts.append(f"测量状态：{period}")

    if metric.gender or metric.age:
        parts.append(f"人群：{_age_band(metric.age)}，{metric.gender or '性别未知'}")

    if metric.height_cm and metric.weight_kg:
        from src.modules.patient.service import bmi_level, calc_bmi

        bmi = calc_bmi(metric.height_cm, metric.weight_kg)
        if bmi:
            parts.append(f"BMI：{bmi}（{LEVEL_LABELS.get(bmi_level(bmi), '')}）")

    if metric.chronic_diseases:
        parts.append(f"既往慢性病：{metric.chronic_diseases}")

    values = [float(v) for v in metric.recent_primary if v is not None]
    if len(values) >= 2:
        avg = sum(values) / len(values)
        parts.append(
            f"近期 {len(values)} 次测量均值 {avg:g}，最低 {min(values):g}，最高 {max(values):g}"
        )

    return (
        f"患者自测：{'；'.join(parts)}。\n"
        f"请说明这个{label}数值有哪些风险、有哪些值得注意的地方。"
    )


def _is_relevant(hit: dict) -> bool:
    """精排分数优先；未装 Reranker 时退回向量余弦相似度。"""
    rerank = hit.get("rerank_score")
    if rerank is not None:
        try:
            return float(rerank) >= MIN_RERANK_SCORE
        except (TypeError, ValueError):
            return False
    score = hit.get("score")
    if score is None:
        return True
    try:
        return float(score) >= MIN_VECTOR_SCORE
    except (TypeError, ValueError):
        return False


async def retrieve_docs(question: str) -> list[dict]:
    """知识库检索：失败（Milvus/Embedding 不可用）返回空列表，不阻断解读。"""
    try:
        from src.infra.embedding import get_embeddings
        from src.infra.milvus_client import get_milvus_client
        from src.knowledge.doc_rag import search_docs_raw

        hits = await asyncio.wait_for(
            search_docs_raw(
                question,
                embedding_model=get_embeddings(),
                milvus_client=get_milvus_client(),
                top_k=RETRIEVAL_TOP_K,
                rerank_top_k=RERANK_TOP_K,
                use_hyde=False,
            ),
            timeout=RETRIEVAL_TIMEOUT,
        )
        kept = [hit for hit in (hits or []) if _is_relevant(hit)]
        if hits and not kept:
            logger.info(f"体征解读：检索到 {len(hits)} 条但均低于相关性门槛，按未覆盖处理")
        return kept
    except Exception as e:
        logger.warning(f"体征解读知识库检索降级: {type(e).__name__}: {e}")
        return []


def format_context(hits: list[dict]) -> str:
    if not hits:
        return ""
    parts = []
    for i, hit in enumerate(hits, 1):
        source = f"[{hit.get('doc_name', '未知文档')}, 第{hit.get('page_number', '?')}页]"
        parts.append(f"片段{i} {source}:\n{hit.get('text', '')}")
    return "\n\n---\n\n".join(parts)


def sources_of(hits: list[dict]) -> list[dict]:
    out, seen = [], set()
    for hit in hits:
        name = hit.get("doc_name")
        if not name or name in seen:
            continue
        seen.add(name)
        out.append({"doc_name": name, "page": hit.get("page_number")})
    return out[:3]


_RULE_TIPS: dict[str, dict[str, list[str]]] = {
    "blood_pressure": {
        "normal": ["保持低盐饮食，每日食盐建议 <5 g", "固定时段、静息 5 分钟后测量，观察长期趋势"],
        "high": ["减少盐分与酒精摄入，控制体重", "每天固定时间测量并记录，连续观察一周"],
        "low": ["起身动作放缓，避免久站后突然站立", "留意是否伴随头晕、乏力、黑矇"],
        "critical": ["立即停止剧烈活动，坐下或平卧休息", "尽快联系医生或前往急诊复测确认"],
    },
    "blood_glucose": {
        "normal": ["保持规律三餐与运动，避免大量含糖饮料"],
        "high": ["减少精制糖与含糖饮料，主食注意粗细搭配", "按医生建议监测空腹与餐后血糖"],
        "low": ["随身备糖果或含糖饮料，出现心慌出汗及时补充", "避免空腹运动或漏餐"],
        "critical": ["立即补充含糖食物并复测", "症状不缓解请尽快就医"],
    },
    "heart_rate": {
        "normal": ["避免咖啡因与剧烈运动后立即测量"],
        "high": ["测量前静息 5 分钟，避免咖啡因与情绪激动", "记录是否伴随心悸、胸闷、气短"],
        "low": ["留意是否伴随头晕、乏力、活动耐量下降", "如为运动人群可结合静息心率对比观察"],
        "critical": ["如伴随晕厥、胸痛请立即就医", "尽快到医院做心电图确认"],
    },
    "weight": {
        "normal": ["每周固定时间、空腹排便后称重，观察趋势"],
        "high": ["控制总热量，增加膳食纤维与蛋白质比例"],
        "low": ["关注近期是否有食欲下降、体重骤降", "必要时排查消化与内分泌原因"],
        "critical": ["短期内明显消瘦建议尽快就医排查原因"],
    },
}


def fallback_text(metric: MetricSnapshot) -> str:
    """模型/知识库都不可用时的规则化文本：只给通用建议，不编造结论。"""
    label = metric_label(metric.metric_type)
    level = metric.level or "normal"
    level_label = LEVEL_LABELS.get(level, "正常")
    tips = _RULE_TIPS.get(metric.metric_type, {}).get(level) or ["保持规律测量，观察趋势变化"]

    if level == "normal":
        summary = f"{label} {format_value(metric)} 在通用参考区间内（{level_label}），继续保持规律监测。"
        risks = ["单次测量不能代表整体状况，需要结合连续趋势判断"]
    elif level == "critical":
        summary = f"{label} {format_value(metric)} 已进入危急值范围，建议尽快就医确认。"
        risks = ["可能存在需要及时处理的风险，不宜仅靠居家观察"]
    else:
        summary = f"{label} {format_value(metric)} 偏离通用参考区间（{level_label}），建议关注并复测。"
        risks = [f"持续{level_label}可能增加相关慢病风险"]

    lines = ["【结论】" + summary, "【可能的风险】", *[f"- {r}" for r in risks]]
    lines += ["【值得注意】", *[f"- {t}" for t in tips]]
    lines += ["【何时就医】", "- 出现胸痛、呼吸困难、意识改变等明显不适请立即就医"]
    return "\n".join(lines)


def _chunks(text: str) -> list[str]:
    """按行切分后整行下发，前端逐段渲染更稳定（不必等整段生成）。"""
    return [line + "\n" for line in text.split("\n")]


async def stream_advice(metric: MetricSnapshot) -> AsyncIterator[dict]:
    """产出 SSE 事件：meta → sources → token* → done。

    检索/模型任一环节失败都不会中断流：要么降级成通用建议，要么给出可读错误。
    """
    yield {
        "type": "meta",
        "metric_id": metric.metric_id,
        "label": metric_label(metric.metric_type),
        "value_text": format_value(metric),
        "level": metric.level,
        "level_label": LEVEL_LABELS.get(metric.level or "normal", "正常"),
        "disclaimer": DISCLAIMER,
    }

    question = build_question(metric)
    hits = await retrieve_docs(question)
    yield {"type": "sources", "sources": sources_of(hits), "grounded": bool(hits)}

    from langchain_core.messages import HumanMessage, SystemMessage

    context = format_context(hits)
    user_parts = [question]
    if context:
        user_parts.append(f"【参考资料】\n{context}")
    else:
        user_parts.append(
            "【参考资料】暂无（知识库未检索到相关内容）：请仅基于通用医学常识作答，"
            "并在【结论】里说明知识库暂未覆盖。"
        )

    started = False
    failed = False
    try:
        llm = get_llm(temperature=0.2)
        async for chunk in llm.astream(
            [SystemMessage(content=SYSTEM_PROMPT), HumanMessage(content="\n\n".join(user_parts))]
        ):
            content = chunk.content
            if isinstance(content, list):
                content = "".join(str(part) for part in content)
            text = str(content or "")
            if text:
                started = True
                yield {"type": "token", "content": text}
    except Exception as e:
        logger.warning(f"体征风险解读生成降级: {type(e).__name__}: {e}")
        failed = True

    if failed and started:
        # 已经出过内容就只提示中断，避免把半截回答和兜底文本混在一起
        yield {"type": "error", "message": "解读生成中断，请重试", "retryable": True}
    elif not started:
        # 一个 token 都没出（模型挂了或空返回）：吐规则化兜底，面板不会空着
        for piece in _chunks(fallback_text(metric)):
            yield {"type": "token", "content": piece}
        yield {"type": "notice", "message": "知识库/模型暂时不可用，以上为通用健康建议。"}

    yield {"type": "done", "grounded": bool(hits)}
