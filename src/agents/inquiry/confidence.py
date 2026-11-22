# src/agents/inquiry/confidence.py

from __future__ import annotations
from loguru import logger
from src.agents.inquiry.state import CandidateDisease, PatientContext


# 易感人群关键词：用来剔除与患者画像冲突的候选疾病。
# 图谱 easy_get 字段覆盖率 100%，但它此前只被当作展示信息，没有参与过滤，
# 结果成人问诊会拿到"婴幼儿脐疝"这类候选并追问出无关问题。
_KIDS_KEYS = ("婴幼儿", "婴儿", "幼儿", "新生儿", "儿童", "小儿", "学龄")
_ELDER_KEYS = ("老年",)
_FEMALE_KEYS = ("女性", "妊娠", "孕产妇", "孕妇")
_MALE_KEYS = ("男性",)


def _is_population_incompatible(easy_get: str, age: int | None, gender: str | None) -> bool:
    """易感人群与患者画像冲突时返回 True；画像缺失的维度不做判断。"""
    eg = easy_get or ""
    if not eg or "无特殊" in eg or "无特定" in eg or "无特发" in eg:
        return False
    if gender in ("男", "女"):
        if gender == "男" and any(k in eg for k in _FEMALE_KEYS):
            return True
        if gender == "女" and any(k in eg for k in _MALE_KEYS):
            return True
    if age is not None:
        if age >= 18 and any(k in eg for k in _KIDS_KEYS):
            return True
        if age < 14 and any(k in eg for k in _ELDER_KEYS):
            return True
    return False


def apply_context_weights(
    candidates: list[CandidateDisease],
    patient_context: PatientContext,  # 患者上下文
    denied_symptoms: list[str],
) -> list[CandidateDisease]:
    """
    在基础置信度上叠加上下文权重，返回重新排序后的候选疾病列表。

    权重规则（来自设计文档）：
      +0.15  用户有该疾病的既往病史（PostgreSQL medical_history）
      +0.10  长期记忆中有相关记录（Milvus long_term_memories）
      +0.05  用户年龄/性别与该疾病易感人群匹配（Patient 表 easy_get 字段）
      -0.20  用户明确否认该疾病的核心症状
             （核心症状 = 只属于该疾病、不属于其他候选疾病的独有症状）
    """
    # 0. 先按易感人群剔除与患者年龄/性别冲突的候选（直接排除，不做降权）
    kept: list[CandidateDisease] = []
    removed_names: list[str] = []
    for c in candidates:
        if _is_population_incompatible(c.easy_get, patient_context.age, patient_context.gender):
            removed_names.append(c.name)
        else:
            kept.append(c)
    if kept:
        if removed_names:
            logger.info("人群过滤剔除 {} 个候选：{}", len(removed_names), removed_names[:5])
        candidates = kept
    elif candidates:
        # 全部冲突时保留原列表：宁可给低置信候选，也不让问诊直接无结果
        logger.warning("人群过滤后候选为空，保留原始列表（患者画像 age={} gender={}）",
                       patient_context.age, patient_context.gender)

    # 计算每个症状出现在几个候选疾病中（用于判断"核心症状"）
    symptom_disease_count: dict[str, int] = {}
    for c in candidates:
        for s in c.all_symptoms:
            symptom_disease_count[s] = symptom_disease_count.get(s, 0) + 1

    for c in candidates:
        score = c.base_confidence

        # +0.15 既往病史命中
        for history_item in patient_context.medical_history:
            if c.name in history_item:
                score += 0.15
                break

        # +0.10 长期记忆命中
        for memory in patient_context.long_term_memories:
            if c.name in memory:
                score += 0.10
                break

        # +0.05 易感人群匹配（简化实现：暂用年龄段粗匹配，后续可接 easy_get 字段）
        # 此处预留接口，实际匹配逻辑需结合 Disease.easy_get 字段内容
        # score += 0.05  # 暂时注释，等 easy_get 字段接入后启用

        # -0.20 核心症状被否认
        # 核心症状 = 只属于该疾病（symptom_disease_count == 1）且被用户否认
        for s in c.all_symptoms:
            if s in denied_symptoms and symptom_disease_count.get(s, 0) == 1:
                score -= 0.20
                break  # 一个核心症状被否认就扣分，不叠加

        # 置信度限制在 [0, 1]
        c.confidence = round(max(0.0, min(1.0, score)), 4)

    # 按最终置信度降序排列
    candidates.sort(key=lambda x: x.confidence, reverse=True)
    return candidates


def check_convergence(
    candidates: list[CandidateDisease],
    current_round: int,
    max_rounds: int = 6,
    confirmed_count: int = 0,
) -> tuple[bool, bool]:
    """
    判断问诊是否可以收敛（输出结论）。

    Args:
        current_round  : 已经完成的追问轮次
        max_rounds     : 轮次上限（用户体验上限，超过就直接出结论）
        confirmed_count: 当前累计确认的症状数，用于识别"信息已饱和"

    Returns:
        (should_conclude, force_conclude)
        should_conclude : True = 可以输出结论
        force_conclude  : True = 是因为达到轮次上限被迫结束（需在结论中标注"信息不足"）
    """
    # 达到轮次上限，强制结束
    if current_round >= max_rounds:
        return True, True

    if not candidates:
        return False, False

    # 信息饱和判定：追问 1 轮仍然只有 1 个确认症状，说明用户要么没有别的症状，
    # 要么表述映射不到图谱词条。这时候继续问下去的只会是候选疾病里
    # 与主诉无关的条（实测"头痛"被连问乏力/腹泻/视物模糊/记性差），
    # 不如按"证据不足"出结论、给出就诊方向，把判断交给线下医生。
    if current_round >= 1 and confirmed_count <= 1:
        logger.info("收敛判定：追问 {} 轮仍只有 {} 个确认症状，按信息饱和收敛",
                    current_round, confirmed_count)
        return True, True

    top1 = candidates[0].confidence

    # 条件0：证据门槛 —— 至少 2 项确认症状同时命中同一疾病，才允许下结论。
    # 图谱里症状关系少的疾病，matched_count/total_symptoms 天然等于 1.0，
    # 只凭"头痛"一个症状就会直接收敛到罕见病（实测收敛到"肢端肥大症性心肌病"）。
    # 单症状不足以支撑诊断，交给追问节点继续问区分度更高的症状。
    if len(candidates[0].matched_symptoms) < 2:
        return False, False

    # 条件1：Top1 置信度 ≥ 70%
    if top1 >= 0.70:
        return True, False

    # 条件2：Top1 与 Top2 置信度差值 ≥ 30%（Top1 明显领先）
    if len(candidates) >= 2:
        top2 = candidates[1].confidence
        if top1 - top2 >= 0.30:
            return True, False


    return False, False