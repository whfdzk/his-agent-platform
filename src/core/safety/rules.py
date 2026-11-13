# src/core/safety/rules.py
#
# 危险信号规则库（可维护的结构化词表 + 阈值判定）。
#
# 与"只靠一次 LLM 判断是否急症"相比，规则库的价值是：
# - 确定性：同一句话永远得到同一结论，可被测试覆盖
# - 可审计：每条命中都有 rule_id，能回溯为什么拦截
# - 可运营：新增/调整规则不需要改 prompt
#
# level 语义：
#   critical → 立即给出急救指引，阻断常规问诊（block）
#   high     → 强烈建议尽快线下就医（warn + 转人工复核）
#   warn     → 提示风险（warn）
#   info     → 仅记录

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass(frozen=True)
class DangerRule:
    """一条危险信号规则。"""

    id: str
    name: str
    category: str                        # emergency / mental / maternal / pediatric / medication / chronic / output
    level: str                           # info / warn / high / critical
    advice: str                          # 命中后给患者的建议
    keywords: tuple[str, ...] = ()       # 关键词（任一命中即算）
    context: tuple[str, ...] = ()        # 上下文词（提供时需再命中任一）
    min_matches: int = 1                 # 需要命中的关键词数量
    patterns: tuple[str, ...] = ()       # 正则
    evaluator: Callable[[str], bool] | None = None
    stage: str = "input"                 # input / output
    action: str = "warn"                 # allow / warn / block / escalate
    requires_review: bool = False
    tags: tuple[str, ...] = field(default_factory=tuple)

    def matches(self, text: str) -> bool:
        if self.evaluator is not None and self.evaluator(text):
            return True
        hits = sum(1 for kw in self.keywords if kw in text)
        if hits >= self.min_matches and hits > 0:
            if not self.context:
                return True
            if any(c in text for c in self.context):
                return True
        for pattern in self.patterns:
            if re.search(pattern, text):
                return True
        return False


# ── 阈值判定器 ────────────────────────────────────────────────────────────
_BP_RE = re.compile(r"(\d{2,3})\s*/\s*(\d{2,3})")


def _blood_pressure_at_least(systolic: int, diastolic: int) -> Callable[[str], bool]:
    def _check(text: str) -> bool:
        for m in _BP_RE.finditer(text):
            try:
                s, d = int(m.group(1)), int(m.group(2))
            except ValueError:
                continue
            if s >= systolic or d >= diastolic:
                return True
        return False
    return _check


_GLUCOSE_RE = re.compile(r"(\d{1,2}(?:\.\d+)?)\s*(?:mmol/L|mmol/l|毫摩尔)")


def _glucose_at_least(value: float) -> Callable[[str], bool]:
    def _check(text: str) -> bool:
        for m in _GLUCOSE_RE.finditer(text):
            try:
                if float(m.group(1)) >= value:
                    return True
            except ValueError:
                continue
        return False
    return _check


DANGER_RULES: tuple[DangerRule, ...] = (
    # ── 循环 / 呼吸 ─────────────────────────────────────────────────────
    DangerRule(
        id="em_chest_pain_red_flag",
        name="胸痛伴危重表现",
        category="emergency",
        level="critical",
        action="block",
        keywords=("胸痛", "胸口痛", "心前区痛", "胸闷痛", "压榨"),
        context=("出汗", "大汗", "呼吸困难", "放射", "左肩", "濒死", "晕倒", "持续不缓解"),
        advice="疑似急性心血管事件，请立即拨打 120，保持静卧、不要自行驾车前往医院。",
        requires_review=True,
        tags=("acs",),
    ),
    DangerRule(
        id="em_chest_pain",
        name="胸痛",
        category="emergency",
        level="high",
        action="warn",
        keywords=("胸痛", "胸口痛", "心前区痛", "压榨性疼痛"),
        advice="胸痛需要尽快排查心脏与肺部问题，建议 2 小时内到急诊或心内科就诊。",
    ),
    DangerRule(
        id="em_dyspnea",
        name="严重呼吸困难",
        category="emergency",
        level="critical",
        action="block",
        keywords=("呼吸困难", "喘不上气", "憋气", "窒息", "喉头水肿", "口唇发紫"),
        advice="存在呼吸衰竭风险，请立即拨打 120，采取半坐位并保持气道通畅。",
        requires_review=True,
    ),
    DangerRule(
        id="em_stroke",
        name="卒中样表现",
        category="emergency",
        level="critical",
        action="block",
        keywords=("口角歪斜", "嘴角歪", "言语不清", "说话不清", "一侧肢体无力", "半身不遂", "偏瘫", "视物重影"),
        advice="疑似脑卒中，溶栓有严格时间窗，请立即拨打 120 并记录症状出现时间。",
        requires_review=True,
    ),
    DangerRule(
        id="em_consciousness",
        name="意识障碍 / 抽搐",
        category="emergency",
        level="critical",
        action="block",
        keywords=("意识不清", "昏迷", "叫不醒", "抽搐", "惊厥", "意识模糊"),
        advice="请立即拨打 120；抽搐时保护头部、不要强行按压肢体或塞入异物。",
        requires_review=True,
    ),
    DangerRule(
        id="em_bleeding",
        name="大出血",
        category="emergency",
        level="critical",
        action="block",
        keywords=("大出血", "呕血", "咯血", "黑便", "血便", "产后出血", "阴道大量出血", "止不住血"),
        advice="存在失血性休克风险，请立即拨打 120，呕血者侧卧防误吸。",
        requires_review=True,
    ),
    DangerRule(
        id="em_anaphylaxis",
        name="严重过敏反应",
        category="emergency",
        level="critical",
        action="block",
        keywords=("过敏性休克", "全身皮疹", "嘴唇肿", "喉咙肿", "注射肾上腺素"),
        context=("呼吸困难", "头晕", "血压低", "意识"),
        advice="疑似过敏性休克，请立即拨打 120；如备有肾上腺素自动注射笔请按说明使用。",
        requires_review=True,
    ),
    DangerRule(
        id="em_poisoning",
        name="中毒 / 药物过量",
        category="emergency",
        level="critical",
        action="block",
        keywords=("中毒", "误服", "农药", "过量服药", "吃了一整瓶", "煤气"),
        advice="请立即拨打 120 并携带药物/毒物包装就诊，不要自行催吐。",
        requires_review=True,
    ),
    DangerRule(
        id="em_severe_trauma",
        name="严重外伤",
        category="emergency",
        level="critical",
        action="block",
        keywords=("车祸", "高处坠落", "大面积烧伤", "严重外伤", "骨折变形", "刀伤"),
        advice="请立即拨打 120，不要随意搬动伤者，出血部位加压包扎。",
        requires_review=True,
    ),
    DangerRule(
        id="em_bp_crisis",
        name="血压危象",
        category="emergency",
        level="critical",
        action="block",
        evaluator=_blood_pressure_at_least(180, 120),
        advice="血压达到危象水平（≥180/120 mmHg），请立即就医；如伴胸痛、头痛、视物模糊请拨打 120。",
        requires_review=True,
    ),
    DangerRule(
        id="em_bp_high",
        name="血压显著升高",
        category="chronic",
        level="high",
        action="warn",
        evaluator=_blood_pressure_at_least(160, 100),
        advice="血压明显升高，建议今天内复测并联系家庭医生调整用药。",
    ),
    DangerRule(
        id="em_glucose_high",
        name="血糖显著升高",
        category="chronic",
        level="high",
        action="warn",
        evaluator=_glucose_at_least(16.7),
        advice="血糖显著升高，若伴口渴、乏力、恶心呕吐需警惕酮症，请尽快就医。",
    ),
    # ── 精神心理 ───────────────────────────────────────────────────────
    DangerRule(
        id="mh_self_harm",
        name="自伤 / 自杀风险",
        category="mental",
        level="critical",
        action="block",
        keywords=("自杀", "不想活", "活着没意思", "自残", "结束生命", "跳楼", "割腕", "安眠药都吃了"),
        advice=(
            "很抱歉听到你正在经历这些。请立即联系身边的人陪伴你，并拨打 24 小时心理援助热线 "
            "400-161-9995 或急救 120。你并不孤单，专业的人可以帮你。"
        ),
        requires_review=True,
        tags=("mental",),
    ),
    DangerRule(
        id="mh_depression",
        name="持续情绪低落",
        category="mental",
        level="warn",
        action="warn",
        keywords=("抑郁", "焦虑", "失眠", "情绪低落", "什么都不想做"),
        advice="持续两周以上的情绪困扰建议到精神心理科评估，必要时可拨打心理援助热线。",
    ),
    # ── 孕产 / 儿科 ────────────────────────────────────────────────────
    DangerRule(
        id="ob_bleeding",
        name="孕期异常出血 / 剧痛",
        category="maternal",
        level="critical",
        action="block",
        keywords=("怀孕", "孕妇", "孕晚期", "孕早期"),
        context=("出血", "剧烈腹痛", "胎动减少", "头痛欲裂", "视物模糊", "抽搐"),
        advice="孕期出现出血、剧烈腹痛或胎动异常，请立即前往产科急诊或拨打 120。",
        requires_review=True,
    ),
    DangerRule(
        id="ped_high_fever",
        name="婴幼儿高热 / 精神差",
        category="pediatric",
        level="high",
        action="warn",
        keywords=("宝宝", "婴儿", "孩子", "小孩"),
        context=("高热", "39度", "40度", "抽搐", "精神差", "不吃奶", "呼吸急促"),
        advice="婴幼儿出现高热伴精神差、拒奶或呼吸急促，请尽快到儿科急诊就诊。",
    ),
    # ── 其它需要尽快就医 ───────────────────────────────────────────────
    DangerRule(
        id="ur_abdominal_pain",
        name="剧烈腹痛",
        category="emergency",
        level="high",
        action="warn",
        keywords=("剧烈腹痛", "肚子疼得厉害", "腹痛难忍", "板状腹"),
        advice="剧烈腹痛可能提示急腹症，请禁食禁水并尽快到急诊就诊。",
    ),
    DangerRule(
        id="ur_fever",
        name="持续高热",
        category="general",
        level="warn",
        action="warn",
        keywords=("高烧不退", "发热三天", "发烧一周", "持续高热"),
        advice="持续高热建议 24 小时内就医，完善血常规等检查。",
    ),
    DangerRule(
        id="ur_hematuria",
        name="血尿 / 黄疸",
        category="general",
        level="warn",
        action="warn",
        keywords=("血尿", "尿血", "皮肤发黄", "眼白发黄", "黄疸"),
        advice="出现血尿或黄疸建议尽快到肾内科/消化内科就诊。",
    ),
    DangerRule(
        id="med_overdose",
        name="用药过量 / 重复用药",
        category="medication",
        level="high",
        action="warn",
        keywords=("吃多了药", "重复吃药", "加量", "超量", "一起吃"),
        context=("退烧药", "降压药", "降糖药", "安眠药", "止痛药", "抗生素"),
        advice="请先停止继续服药，携带药品包装尽快咨询药师或就诊，必要时拨打 120。",
        requires_review=True,
    ),
    # ── 输出审核（stage=output） ───────────────────────────────────────
    DangerRule(
        id="out_absolute_diagnosis",
        name="绝对化诊断结论",
        category="output",
        level="high",
        action="warn",
        stage="output",
        keywords=("你患的是", "确诊为", "可以确定是", "百分之百是", "肯定是"),
        advice="AI 不能给出确诊结论，需由医生结合检查判断。",
    ),
    DangerRule(
        id="out_dosage",
        name="具体用药剂量建议",
        category="output",
        level="high",
        action="warn",
        stage="output",
        patterns=(
            r"(每次|每日|每天|一天)\s*(服用|吃|用)\s*\d+\s*(片|粒|袋|支)",
            r"剂量\s*(调整|增加|改为)\s*到?\s*\d+\s*(mg|毫克|片)",
        ),
        advice="AI 不提供具体剂量调整，请遵医嘱或咨询药师。",
    ),
    DangerRule(
        id="out_cure_guarantee",
        name="疗效保证",
        category="output",
        level="high",
        action="warn",
        stage="output",
        keywords=("保证治愈", "包治", "根治", "一定能好", "永不复发"),
        advice="任何治疗都不能保证治愈，请以医生面诊意见为准。",
    ),
    DangerRule(
        id="out_self_harm_method",
        name="自伤方法描述",
        category="output",
        level="critical",
        action="block",
        stage="output",
        keywords=("自杀方法", "怎么自杀", "如何结束生命", "致死剂量"),
        advice="不能提供任何自伤相关信息。",
        requires_review=True,
    ),
    DangerRule(
        id="out_stop_medication",
        name="擅自停药建议",
        category="output",
        level="warn",
        action="warn",
        stage="output",
        keywords=("立即停药", "把药停了", "不用再吃"),
        context=("降压药", "降糖药", "抗凝", "激素", "抗癫痫"),
        advice="调整或停用长期药物必须由医生评估。",
    ),
)


# 输出未包含免责声明时的提示（不作为阻断，只追加）
DISCLAIMER = "以上内容仅用于健康科普，不能替代医生面诊与检查结论；如有不适请及时就医。"
EMERGENCY_DISCLAIMER = "如症状加重或出现意识改变，请立即拨打 120。"


def rules_for(stage: str) -> tuple[DangerRule, ...]:
    return tuple(r for r in DANGER_RULES if r.stage == stage)
