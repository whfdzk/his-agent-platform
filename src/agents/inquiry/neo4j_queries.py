# src/agents/inquiry/neo4j_queries.py

from __future__ import annotations
from loguru import logger
from neo4j import AsyncDriver
from src.agents.inquiry.state import CandidateDisease


async def query_candidate_diseases(
    confirmed_symptoms: list[str],
    neo4j_driver: AsyncDriver,
    top_k: int = 10,
) -> list[CandidateDisease]:
    """
    根据已确认症状列表，从 Neo4j 查询候选疾病。
    按基础置信度（命中症状数 / 该疾病总症状数）降序排列，取 Top K。
    """
    if not confirmed_symptoms:
        return []

    cypher = """
    MATCH (d:Disease)-[:HAS_SYMPTOM]->(s:Symptom)
    WHERE s.name IN $confirmed_symptoms
    WITH d, collect(s.name) AS matched_symptoms, count(s) AS matched_count
    MATCH (d)-[:HAS_SYMPTOM]->(all_s:Symptom)
    WITH d, matched_symptoms, matched_count, count(all_s) AS total_symptoms
    ORDER BY toFloat(matched_count) / total_symptoms DESC
    LIMIT $top_k
    RETURN
        d.name AS disease,
        matched_symptoms,
        matched_count,
        total_symptoms,
        toFloat(matched_count) / total_symptoms AS base_confidence
    """

    async with neo4j_driver.session() as session:
        result = await session.run(
            cypher,
            confirmed_symptoms=confirmed_symptoms,
            top_k=top_k,
        )
        records = await result.data()

    candidates = []
    # 用 F1 替代单一的 matched/total：后者只看"疾病表现被覆盖多少"，
    # 症状关系少的疾病天然拿满分（1/1=1.0），排序失去区分度。
    # 加上"用户描述被解释多少"这一项后，多症状命中才会排到前面。
    confirmed_count = max(1, len(confirmed_symptoms))
    for r in records:
        matched = r["matched_count"]
        total = r["total_symptoms"] or 1
        precision = min(1.0, matched / confirmed_count)  # 用户症状被该疾病解释的比例
        recall = matched / total                         # 该疾病表现被覆盖的比例
        f1 = 0.0 if precision + recall == 0 else 2 * precision * recall / (precision + recall)
        # 本体完整度权重：图谱里有 741 个疾病只挂了 1-2 个症状（数据缺失），
        # 它们靠"1/1=1.0"的召回率恒久占据榜首，但只凭一个症状不可能确诊。
        # 症状数 ≥4 时不打折，越少打折越狠（1 条→0.25，2 条→0.5，3 条→0.75）。
        completeness = min(1.0, total / 4.0)
        confidence = f1 * completeness
        candidates.append(CandidateDisease(
            name=r["disease"],
            base_confidence=round(confidence, 4),
            confidence=round(confidence, 4),  # 初始值，后续加权调整
            matched_symptoms=r["matched_symptoms"],
            all_symptoms=[],   # 由 enrich_candidate_details 补充
            department="",
            checks=[],
            complications=[],
        ))

    logger.debug(f"Neo4j 候选疾病: {[c.name for c in candidates]}")
    return candidates


async def enrich_candidate_details(
    candidates: list[CandidateDisease],
    neo4j_driver: AsyncDriver,
) -> list[CandidateDisease]:
    """
    补充候选疾病的完整信息：全部症状、建议科室、建议检查、并发症。
    在首次查到候选疾病后调用一次，后续轮次复用缓存在 state 里的数据。
    """
    if not candidates:
        return candidates

    disease_names = [c.name for c in candidates]

    # 查全部症状（顺带取易感人群 easy_get，供按患者年龄/性别过滤候选）
    symptoms_cypher = """
    MATCH (d:Disease)-[:HAS_SYMPTOM]->(s:Symptom)
    WHERE d.name IN $names
    RETURN d.name AS disease, collect(s.name) AS symptoms, max(d.easy_get) AS easy_get
    """
    # 查科室
    dept_cypher = """
    MATCH (d:Disease)-[:BELONGS_TO]->(dept:Department)
    WHERE d.name IN $names
    RETURN d.name AS disease, dept.name AS department
    """
    # 查检查项目
    check_cypher = """
    MATCH (d:Disease)-[:NEED_CHECK]->(c:Check)
    WHERE d.name IN $names
    RETURN d.name AS disease, collect(c.name) AS checks
    """
    # 查并发症
    comp_cypher = """
    MATCH (d:Disease)-[:ACOMPANY_WITH]->(comp:Disease)
    WHERE d.name IN $names
    RETURN d.name AS disease, collect(comp.name) AS complications
    """

    async with neo4j_driver.session() as session:
        symptoms_result = await (await session.run(symptoms_cypher, names=disease_names)).data()
        dept_result     = await (await session.run(dept_cypher,     names=disease_names)).data()
        check_result    = await (await session.run(check_cypher,    names=disease_names)).data()
        comp_result     = await (await session.run(comp_cypher,     names=disease_names)).data()

    # 构建查找字典
    symptoms_map    = {r["disease"]: r["symptoms"]      for r in symptoms_result}
    easy_get_map    = {r["disease"]: (r["easy_get"] or "") for r in symptoms_result}
    dept_map        = {r["disease"]: r["department"]    for r in dept_result}
    check_map       = {r["disease"]: r["checks"]        for r in check_result}
    comp_map        = {r["disease"]: r["complications"] for r in comp_result}

    for c in candidates:
        c.all_symptoms  = symptoms_map.get(c.name, [])
        c.easy_get      = easy_get_map.get(c.name, "")
        c.department    = dept_map.get(c.name, "")
        c.checks        = check_map.get(c.name, [])
        c.complications = comp_map.get(c.name, [])

    return candidates


# 图谱里混着体征描述句（"一侧乳头萎缩，..."）和导入噪声词（"驻站医"），
# 它们天然只关联 1-2 个疾病；而追问策略是"出现次数越少区分度越高"，
# 于是这些词永远排在最前，问出来患者根本听不懂。追问只挑患者能用一句话回答的短词。
_NOISE_SYMPTOMS = {"驻站医", "测试"}
# 危重体征：图谱里作为某病的 HAS_SYMPTOM 挂着，但它描述的是"已经送进急诊"的状态，
# 不该拿去向门诊患者逐条确认——实测病人在描述头痛时被问"有没有休克"、"有没有对侧肢体偏瘫"。
_SEVERE_SYMPTOMS = {
    "休克", "昏迷", "意识障碍", "意识模糊", "谵妄", "呼之不应",
    "偏瘫", "瘫痪", "截瘫", "大出血", "猝死",
    "呼吸衰竭", "心力衰竭", "肾衰竭", "肝衰竭", "多器官功能衰竭",
    "抽搐", "惊厥", "脑疝", "窒息",
}
_SYMPTOM_PUNCT = set('，。、；：（）()？！…“”‘’"·,.;:?!<>[]{}')


def _is_askable(symptom: str) -> bool:
    """判断一个症状名是否适合拿去问患者。"""
    s = (symptom or "").strip()
    if not s or s in _NOISE_SYMPTOMS:
        return False
    if s in _SEVERE_SYMPTOMS or any(k in s for k in ("衰竭", "偏瘫", "休克", "昏迷")):
        return False
    if not 2 <= len(s) <= 8:      # 太短没有信息量，太长是体征描述句
        return False
    if any(ch in _SYMPTOM_PUNCT for ch in s):
        return False
    return True


async def get_pending_symptoms(
    candidates: list[CandidateDisease],
    confirmed_symptoms: list[str],
    denied_symptoms: list[str],
    asked_symptoms: list[str],
) -> list[tuple[str, int]]:
    """从头部候选疾病的症状集合中，提取尚未确认/否认/追问过的症状。
    返回 [(症状名, 出现在几个候选疾病中)]，按出现次数降序排列。

    只取排名前 3 的候选，并按"多个头部候选共有"排序：这类症状答"有"能抬高精确率，
    答"没有"能一次性排除一批疾病。反过来，按"出现次数越少越优先"会挑出只属于长尾
    罕见疾病的低频症状，把追问带偏（实测问出"尿失禁""驻站医"这类与主诉无关的问题）。
    """
    already_known = set(confirmed_symptoms) | set(denied_symptoms) | set(asked_symptoms)

    # 追问依据只取"本体表型最完整"的头部候选。图谱里有大量本体残缺的疾病（如
    # 膀胱咳只挂 3 个症状、陈旧性肺结核只挂 1 个），它们的"独有体征"往往是化验指标
    # （粒细胞减少）或无关表现（尿失禁），按共有症状问出来患者根本答不了。
    # 同时只让与 top1 分数接近的候选参与，避免长尾噪声疾病左右话术。
    head = candidates[0].confidence if candidates else 0.0
    near_head = [c for c in candidates[:3] if c.confidence >= head * 0.85] or candidates[:1]
    rich = [c for c in near_head if len(c.all_symptoms) >= 4]
    focus = sorted(rich or near_head, key=lambda c: -len(c.all_symptoms))[:2]

    symptom_count: dict[str, int] = {}
    for c in focus:
        for s in c.all_symptoms:
            if s not in already_known and _is_askable(s):
                symptom_count[s] = symptom_count.get(s, 0) + 1

    # 头部候选越共有的越先问；出现次数相同时，短词优先（更好回答）
    sorted_symptoms = sorted(symptom_count.items(), key=lambda x: (-x[1], len(x[0]), x[0]))
    return sorted_symptoms