# src/core/memory_schema.py
#
# 长期记忆的结构化字段定义：把自由文本记忆归到「过敏史 / 疾病史 / 用药史 / 其他」四类。
#
# 为什么需要：原来所有记忆都是 {"content": "..."} 一坨自由文本，检索只能按语义相似度捞，
# 没法表达"用药问题必须优先看到过敏史"这种业务优先级，也没法做按类别的维护。
#
# 分类有两处来源：
# 1. 模型调用 save_memory 时显式传 kind（推荐，语义最准）
# 2. 缺失或非法时用关键词规则兜底（不依赖模型，保证字段一定有值）

from __future__ import annotations

MEMORY_KINDS = ("allergy", "condition", "medication", "other")

KIND_LABELS = {
    "allergy": "过敏史",
    "condition": "疾病史",
    "medication": "用药史",
    "other": "其他",
}

# 注入优先级：过敏史永远排最前（用药禁忌直接关系到安全），其次是正在用的药，再到基础疾病
KIND_PRIORITY = {"allergy": 0, "medication": 1, "condition": 2, "other": 3}

_ALLERGY_WORDS = ("过敏", "皮疹", "荨麻疹", "皮试", "不良反应", "起疹子", "发痒")
_MEDICATION_WORDS = ("服用", "吃药", "用药", "在吃", "长期吃", "注射", "打点滴", "服药", "贴", "喷")
_CONDITION_WORDS = (
    "病史", "患有", "确诊", "手术", "高血压", "糖尿病", "冠心病", "哮喘",
    "肾病", "肝炎", "脑梗", "心梗", "肿瘤", "甲亢", "贫血", "痛风", "慢阻肺",
)


def normalize_kind(kind: str | None) -> str:
    """把模型传进来的 kind 规整成合法值；非法返回空串，交给 infer_kind 兜底。"""
    k = (kind or "").strip().lower()
    return k if k in MEMORY_KINDS else ""


def infer_kind(content: str) -> str:
    """关键词兜底分类。过敏优先：'我对阿莫西林过敏起皮疹' 应归过敏史而不是用药史。"""
    text = content or ""
    if any(w in text for w in _ALLERGY_WORDS):
        return "allergy"
    if any(w in text for w in _MEDICATION_WORDS):
        return "medication"
    if any(w in text for w in _CONDITION_WORDS):
        return "condition"
    return "other"


def resolve_kind(content: str, kind: str | None) -> str:
    """优先用模型给的合法 kind，否则按内容推断。"""
    return normalize_kind(kind) or infer_kind(content)


def label_of(kind: str | None) -> str:
    return KIND_LABELS.get(normalize_kind(kind), KIND_LABELS["other"])


def priority_of(kind: str | None) -> int:
    return KIND_PRIORITY.get(normalize_kind(kind), KIND_PRIORITY["other"])

