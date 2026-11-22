# src/knowledge/entity_alias.py
#
# 实体归一化：把"用户怎么说"翻译成"图谱怎么存"。
#
# 为什么需要：
#   图谱里存的是具体药名（缬沙坦胶囊、盐酸贝那普利片）和标准症状词（咳嗽），
#   而用户说的是药物类别（ACEI、ARB、CCB）和口语症状（干咳、心慌）。
#   直接拿这些词去 MATCH 节点，命中率恒为 0——表现为"图谱通道永远为空"。
#
# 做法：只补映射信息，不改原实体。NL2Cypher 拿到映射后自己决定用 CONTAINS 还是换词，
#   保留模型的选择空间（不同问题该走哪条路径不一样）。

from __future__ import annotations

import re

# 药物类别 → 图谱药名的后缀 / 关键词
# 图谱 Drug 节点存的是通用名（"缬沙坦胶囊"），所以按后缀匹配比精确匹配可靠
DRUG_CLASS_PATTERNS: dict[str, list[str]] = {
    "ACEI": ["普利"],
    "ACE-I": ["普利"],
    "血管紧张素转换酶抑制剂": ["普利"],
    "普利类": ["普利"],
    "ARB": ["沙坦"],
    "血管紧张素受体拮抗剂": ["沙坦"],
    "沙坦类": ["沙坦"],
    "CCB": ["地平"],
    "钙通道阻滞剂": ["地平"],
    "地平类": ["地平"],
    "β受体阻滞剂": ["洛尔"],
    "β-blocker": ["洛尔"],
    "β受体阻断剂": ["洛尔"],
    "洛尔类": ["洛尔"],
    "利尿剂": ["噻嗪", "呋塞米", "螺内酯"],
    "噻嗪类": ["噻嗪"],
    "他汀类": ["他汀"],
    "双胍类": ["二甲双胍"],
}

# 口语症状 / 近似说法 → 图谱 Symptom 节点名
# 只收高频、且图上确实存在对应节点的；映射到不存在的词没有意义
SYMPTOM_ALIASES: dict[str, str] = {
    "干咳": "咳嗽",
    "刺激性干咳": "咳嗽",
    "咳嗽不止": "咳嗽",
    "一直咳嗽": "咳嗽",
    "肚子疼": "腹痛",
    "肚子痛": "腹痛",
    "心慌": "心悸",
    "心里发慌": "心悸",
    "心跳快": "心悸",
    "气短": "呼吸困难",
    "喘不上气": "呼吸困难",
    "喘不过气": "呼吸困难",
    "拉肚子": "腹泻",
    "想吐": "恶心",
    "睡不着": "失眠",
}


def _clean(name: str) -> str:
    return re.sub(r"[（(].*?[）)]", "", str(name or "")).strip()


def normalize_entities(entities: dict) -> dict:
    """在原实体基础上补两类映射，不改动原值。

    返回新增字段：
      drug_class_patterns: {"ACEI": ["普利"]}      药名后缀，供 CONTAINS 使用
      symptom_canonical:   {"干咳": "咳嗽"}        标准症状词
    """
    entities = dict(entities or {})

    drug_class_patterns: dict[str, list[str]] = {}
    for drug in entities.get("drugs") or []:
        key = _clean(drug)
        pattern = DRUG_CLASS_PATTERNS.get(key) or DRUG_CLASS_PATTERNS.get(key.upper())
        if pattern:
            drug_class_patterns[drug] = pattern

    symptom_canonical: dict[str, str] = {}
    for symptom in entities.get("symptoms") or []:
        canon = SYMPTOM_ALIASES.get(_clean(symptom))
        if canon:
            symptom_canonical[symptom] = canon

    if drug_class_patterns:
        entities["drug_class_patterns"] = drug_class_patterns
    if symptom_canonical:
        entities["symptom_canonical"] = symptom_canonical
    return entities
