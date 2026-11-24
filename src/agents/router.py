# src/agents/router.py
#
# 入口路由：先判定"这是不是一个单域知识问题"。
#
# 背景（实测 r5-1）：supervisor 一轮里调了 13 次模型，在 drug_agent / knowledge_agent
# 之间来回派单，最后撞上 recursion_limit=16 直接 500。"阿司匹林有什么副作用"明明是
# 单域问题，却被当成多域编排来处理。成熟做法是入口先分一次类：单域直接交给对应
# 助手（拿到答案就返回），只有多诉求才进 supervisor。
#
# 这里只把两类问题放行到直连：药品（drug）与医学知识（knowledge）。问诊、报告、
# 健康、就医、运营仍走 supervisor —— 它们有多轮状态（问诊图、确认卡片、档案写入），
# 直接绕过会丢掉这些副作用，不值得在一次试验里一起改。
#
# 判定顺序：规则优先（零成本、可解释），规则无结论才调一次小模型。

from __future__ import annotations

import re
from typing import Literal

from langchain_core.messages import SystemMessage
from loguru import logger
from pydantic import BaseModel, Field

from src.core.config import get_llm

# ── 规则表 ────────────────────────────────────────────────────────────────
# 命中即直连；两边都命中、或命中"必须走编排"的词，直接判 multi。

_DRUG_KEYWORDS = (
    "说明书", "副作用", "不良反应", "禁忌", "相互作用", "一起吃", "能同服",
    "剂量", "用法用量", "用药", "吃药", "停药", "过敏能不能", "孕妇能不能吃",
    "哺乳期能不能", "过量", "中毒",
)

_KNOWLEDGE_KEYWORDS = (
    "指南", "是什么", "为什么", "有哪些", "早期症状", "临床表现", "指标",
    "正常值", "参考范围", "饮食", "注意事项", "原理", "区别", "并发症",
    "会传染", "怎么预防", "病因", "发病机制",
)

# 这些词意味着要做动作或多轮状态，必须回到 supervisor / 对应专门流程
_FORCE_ORCHESTRATION = (
    "挂什么科", "挂哪个科", "挂号", "预约", "报告", "帮我记", "记录下来",
    "提醒我", "我的档案", "我的病历", "就诊量", "收入", "排名",
    "转人工", "投诉",
)

# 常见药名（用于"阿司匹林有什么副作用"这类没写"副作用"但明显是药名的问法）
_DRUG_NAMES = (
    "阿司匹林", "布洛芬", "对乙酰氨基酚", "扑热息痛", "阿莫西林", "头孢",
    "二甲双胍", "胰岛素", "氨氯地平", "缬沙坦", "阿托伐他汀", "奥美拉唑",
    "华法林", "氯吡格雷", "地塞米松", "泼尼松", "蒙脱石散", "左氧氟沙星",
    "阿奇霉素", "甲硝唑", "维生素", "钙片", "布地奈德", "沙丁胺醇",
)

_SINGLE_AGENT = Literal["drug", "knowledge"]
_MODE = Literal["single", "multi"]


class RouteDecision(BaseModel):
    """一次路由的结论。single 时 agent 必填。"""

    mode: _MODE = Field(description="single=单域问题直连助手，multi=需要 supervisor 编排")
    agent: _SINGLE_AGENT | None = Field(default=None, description="single 时要直连的助手")
    reason: str = Field(default="", description="一句话理由，用于 trace 排查")


_ROUTE_PROMPT = """你是一个问题分类器，只做一件事：判断用户这句话是"单一领域的医学知识问题"还是"需要编排"。

可选结论：
- single + drug：问某个药/某类药的说明书、副作用、不良反应、禁忌、能不能一起吃、剂量、停药、过敏风险。
- single + knowledge：问疾病/症状/指标/饮食/指南/原理这类纯知识，不涉及具体用药。
- multi：需要多步动作或多个邻域，例如挂号/预约、解读报告、记录指标、运营数据、转人工，
  或者一句话里同时有用药和就诊/记录两个诉求。

拿不准就选 multi。只输出 JSON，不要解释。

用户问题："""


def _hits(text: str, words: tuple[str, ...]) -> list[str]:
    return [w for w in words if w in text]


def classify_by_rules(message: str) -> RouteDecision | None:
    """零成本的规则判定；给不出结论返回 None。"""
    text = message.strip()
    if not text:
        return RouteDecision(mode="multi", reason="空输入，交回 supervisor")

    if _hits(text, _FORCE_ORCHESTRATION):
        return RouteDecision(mode="multi", reason="命中动作类关键词")

    drug_hits = _hits(text, _DRUG_KEYWORDS)
    name_hits = _hits(text, _DRUG_NAMES)
    know_hits = _hits(text, _KNOWLEDGE_KEYWORDS)

    # 只要问到具体药名（"布洛芬怎么吃"没有"副作用"这类词），就是药品问题。
    # 药名 + 知识词（"阿司匹林有什么常见副作用"）同样落在药品侧。
    if drug_hits or name_hits:
        return RouteDecision(mode="single", agent="drug", reason=f"药品关键词：{drug_hits or name_hits}")

    if know_hits:
        return RouteDecision(mode="single", agent="knowledge", reason=f"知识关键词：{know_hits}")

    return None


async def classify_by_llm(message: str) -> RouteDecision:
    """规则无结论时调一次小模型（关思考、限输出），失败按 multi 处理。"""
    try:
        llm = get_llm(temperature=0.0, thinking=False, max_tokens=120)
        structured = llm.with_structured_output(RouteDecision)
        decision: RouteDecision = await structured.ainvoke(
            [SystemMessage(content=_ROUTE_PROMPT + message)]
        )
        if decision.mode == "single" and decision.agent is None:
            return RouteDecision(mode="multi", reason="single 但没给 agent，按 multi 处理")
        return decision
    except Exception as e:  # 路由失败绝不能把请求打死，回退到 supervisor
        logger.warning(f"路由分类失败，回退 supervisor: {type(e).__name__}: {e}")
        return RouteDecision(mode="multi", reason=f"分类异常 {type(e).__name__}")


async def route_message(message: str) -> RouteDecision:
    """入口路由总入口：规则 → 小模型 → multi 兜底。"""
    rule = classify_by_rules(message)
    if rule is not None:
        logger.info(f"[router] 规则判定 mode={rule.mode} agent={rule.agent} 理由={rule.reason}")
        return rule

    decision = await classify_by_llm(message)
    logger.info(f"[router] 模型判定 mode={decision.mode} agent={decision.agent} 理由={decision.reason}")
    return decision


# 便于单测：判断是否带明确的药品名
def has_drug_name(message: str) -> bool:
    return bool(re.search("|".join(map(re.escape, _DRUG_NAMES)), message))
