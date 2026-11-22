import uuid
from typing import Any

from loguru import logger
from pydantic import BaseModel, Field

from src.agents.builder import build_worker_agent, get_runtime_agent
from src.agents.inquiry.state import InquiryHandoffPayload
from src.core.agent_registry import AgentRuntimeConfig, get_active_config
from src.core.config import get_llm
# ── 预约确认判定 ──────────────────────────────────────────────────────────
# 出结论时绝不能顺手把预约建掉：患者还没说"要"，后台就先落了一条挂号记录，
# 和话术里"请问是否需要帮您预约"自相矛盾。这里用关键词判定确认/拒绝，
# 判不出来就保持原状态、让对话继续，等患者明确表态。
_DENY_WORDS = (
    "不用", "不需要", "不必", "不要", "先不", "暂时不", "算了", "不预约",
    "不挂号", "别预约", "过几天", "再说",
)
# 疑问句不是同意："请直接告诉我该挂哪个科" 里含"要""挂"，被旧词表判成确认，
# 结果用户在问科室、后台已经把号挂了（实测产出 APT8552C754 并落库）。
_QUESTION_MARKERS = (
    "?", "？", "吗", "哪个", "哪科", "哪一科", "什么科", "怎么", "如何",
    "是否", "能不能", "可否", "要不要", "该挂", "挂哪",
)
_CONFIRM_PHRASES = (
    "帮我预约", "帮我挂号", "帮我挂个号", "给我预约", "要预约", "需要预约",
    "预约吧", "预约一下", "确认预约", "安排预约", "要挂号", "就这个",
    # B2 补齐口语化表达：旧词表只认"帮我预约"式，实测漏判
    # 「帮忙挂个号吧」「可以，帮我约周三上午」「约一下明天上午」
    # 漏判比误判更糟——患者以为约上了，后台没建，界面也没提示。
    "挂个号", "挂个", "约一下", "约个", "帮我约", "给我挂", "排个号",
    "加个号", "留个号", "就按这个", "就这样", "听你的",
)
_BARE_CONFIRM = {"需要", "要", "好", "好的", "可以", "行", "确认", "嗯", "是", "预约"}
# 命中确认短语但同时出现这些词，说明患者在问"办事细节"（"挂个号要带什么"），
# 不是表态。不直接判 True，转给 LLM 兜底细分。
_ASKING_DETAIL = (
    "带什么", "需要带", "准备", "流程", "费用", "多少钱", "几点", "多久",
    "怎么走", "在哪", "地址", "注意什么", "注意事项",
)


def is_appointment_confirmation(text: str) -> bool | None:
    """判断患者这句话是不是在回应"要不要预约挂号"。

    Returns:
        True  = 明确要预约
        False = 明确不预约
        None  = 既没答应也没拒绝（例如反问病情、问注意事项），交给正常对话
    """
    t = (text or "").strip()
    if not t:
        return None
    # 疑问优先：问"该挂哪个科""要不要预约"都不算表态，保持原状态等下一轮
    if any(m in t for m in _QUESTION_MARKERS):
        return None
    # 拒绝优先：避免"不用预约了"里的"预约"误判成确认
    if any(w in t for w in _DENY_WORDS):
        return False
    if any(p in t for p in _CONFIRM_PHRASES):
        if any(d in t for d in _ASKING_DETAIL):
            return None
        return True
    # 整句就是一个纯确认词（"需要" / "好的" / "预约"），才算同意
    if len(t) <= 6 and t in _BARE_CONFIRM:
        return True
    return None


_CONFIRM_LLM_PROMPT = """上一轮助手问患者：「是否需要现在帮您预约挂号？」。
患者这一句回复是：{text}

判断患者的意图，只输出下面三个词之一，不要任何解释：
AGREE  —— 明确同意现在就预约挂号
DENY   —— 明确表示不预约、不要挂
OTHER  —— 没有表态（在问病情、科室、注意事项、流程、费用，或答非所问）"""


async def judge_appointment_confirmation(text: str, *, allow_llm: bool = True) -> bool | None:
    """关键词优先，判不出来再走一次轻量 LLM 分类。

    关键词命中直接返回（快路径覆盖"需要/好的/约一下明天"等常见表达）；
    只有真正含糊的句子（"那我周三过去看看"）才花一次 LLM 调用。
    LLM 不可用 / 超时 / 输出不可识别时回落 None，保持原状态等下一轮，
    宁可多问一句，也不擅自建预约。
    """
    kw = is_appointment_confirmation(text)
    if kw is not None or not allow_llm:
        return kw
    t = (text or "").strip()
    if not t or len(t) > 80:
        return None
    try:
        from src.core.concurrency import guarded

        llm = get_llm(temperature=0)
        resp = await guarded(llm.ainvoke(_CONFIRM_LLM_PROMPT.format(text=t)), timeout=8)
        raw = getattr(resp, "content", resp)
        if isinstance(raw, list):
            raw = "".join(str(p) for p in raw)
        verdict = str(raw or "").strip().upper()
        if verdict.startswith("AGREE"):
            logger.info(f"预约确认 LLM 兜底判定=AGREE text={t[:30]}")
            return True
        if verdict.startswith("DENY"):
            logger.info(f"预约确认 LLM 兜底判定=DENY text={t[:30]}")
            return False
        return None
    except Exception as e:
        logger.warning(f"预约确认意图 LLM 兜底失败（按未表态处理）: {type(e).__name__}: {e}")
        return None


INQUIRY_SYSTEM_PROMPT = """你是医疗平台的智慧问诊助手。

你的职责：
1. 根据患者描述的症状，进行智能分诊
2. 判断患者应该挂哪个科室
3. 如有必要，主动追问关键症状信息
4. 评估病情紧急程度（紧急/普通/可预约）

回复格式：
- 分诊科室：xxx科
- 紧急程度：xxx
- 建议：xxx

注意：你只负责分诊建议，不做最终诊断。"""


_AGENT_CODE = "inquiry"
_DEFAULT_TEMPERATURE = 0.3
# 当前阶段：无工具，纯 LLM 推理
# 未来可添加：患者历史记录查询工具、HyDE 检索工具、预约挂号工具
_DEFAULT_TOOLS: list[Any] = []


def _build(cfg: AgentRuntimeConfig) -> Any:
    return build_worker_agent(
        cfg,
        default_prompt=INQUIRY_SYSTEM_PROMPT,
        default_tools=_DEFAULT_TOOLS,
        default_temperature=_DEFAULT_TEMPERATURE,
    )


def create_inquiry_agent():
    """按当前配置新建一个实例（不写缓存，便于测试/排查）。"""
    return _build(get_active_config(_AGENT_CODE))


def get_inquiry_agent():
    """取单例：Agent 配置保存/发布后自动重建。"""
    return get_runtime_agent(_AGENT_CODE, _build)

# ── 结构化输出 Schema ─────────────────────────────────────────────────────

class AppointmentResult(BaseModel):
    """LLM 生成的模拟挂号预约结果"""
    doctor_name: str = Field(description="医生姓名及职称，如：张伟（主任医师）")
    department: str = Field(description="就诊科室")
    date: str = Field(description="就诊日期，格式 YYYY-MM-DD，取明天或后天")
    time_slot: str = Field(description="就诊时段，如：08:00-12:00 或 14:00-17:30")
    fee: float = Field(description="挂号费（元），主任医师 50 元，其他 30 元")
    queue_number: int = Field(description="就诊序号，1~30 之间的整数")


# ── 核心函数 ──────────────────────────────────────────────────────────────

def _fallback_appointment(payload: InquiryHandoffPayload) -> AppointmentResult:
    """LLM 不可用时的本地兜底：按会话/诊断做确定性随机，保证挂号流程不中断。"""
    import datetime
    import random

    seed = f"{payload.session_id}:{payload.primary_disease}:{payload.department}"
    rng = random.Random(seed)
    visit_date = datetime.date.today() + datetime.timedelta(days=rng.randint(1, 2))
    name = rng.choice(["张伟", "李静", "王建国", "刘芳", "陈志远", "赵慧敏"])
    return AppointmentResult(
        doctor_name=f"{name}（主任医师）",
        department=payload.department or "内科",
        date=visit_date.isoformat(),
        time_slot=rng.choice(["08:00-12:00", "14:00-17:30"]),
        fee=50.0,
        queue_number=rng.randint(1, 30),
    )


async def mock_create_appointment(payload: InquiryHandoffPayload) -> AppointmentResult:
    """
    用 LLM 结构化输出生成一条模拟预约数据。
    输入问诊结论，输出符合场景的预约信息，无需维护任何模拟数据库。
    网络抖动/网关不可用时降级为本地生成，保证挂号移交不中断。
    """
    # 统一走 get_llm 工厂：自动读取 BASE_URL_CHAT / API Key / 超时重试配置，
    # 避免直连 api.openai.com 导致 ConnectError
    llm = get_llm(temperature=0.7)  # 适当随机，让每次生成的医生/时间略有不同
    structured_llm = llm.with_structured_output(AppointmentResult)

    prompt = f"""根据以下问诊结论，生成一条合理的挂号预约信息。

问诊结论：
- 主诊断：{payload.primary_disease}
- 建议科室：{payload.department}
- 确认症状：{', '.join(payload.confirmed_symptoms[:5])}

要求：
- 医生姓名用中文常见姓名，附上职称
- 日期取明天或后天（今天是 {__import__('datetime').date.today()}）
- 时段选上午或下午
- 挂号费主任医师 50 元，其他 30 元

以 JSON 对象格式返回以上字段（网关的 response_format=json_object 要求提示词里出现 "json" 字样，否则直接 400）。"""

    # 第一优先：instructor 走 JSON 模式 + 校验失败回灌重试
    # 这条路径能真正拿到模型生成的医生/时段，而不是本地随机兜底
    try:
        from src.core.structured import extract_structured

        return await extract_structured(
            AppointmentResult,
            prompt,
            system="你是医院挂号台助手，请以 JSON 格式输出一条挂号预约信息。",
            temperature=0.7,
            max_retries=2,
        )
    except Exception as e:
        logger.warning(
            f"instructor 结构化生成失败，回退 LangChain 结构化输出: {type(e).__name__}: {e}"
        )

    # 第二优先：原来的 with_structured_output（网关 function-calling 可用时）
    try:
        return await structured_llm.ainvoke(prompt)
    except Exception as e:
        logger.warning(f"预约信息 LLM 生成失败，使用本地兜底数据: {type(e).__name__}: {e}")
        return _fallback_appointment(payload)

# 测试环境用，不依赖真实挂号系统
async def handle_handoff(payload: InquiryHandoffPayload, persist: bool = True) -> tuple[str, dict]:
    """
    接收问诊移交数据包，生成模拟预约结果并返回确认文案。

    ⚠ B2 起本函数**只生成文案，永不落库**。挂号落库只有一个入口：
    clinical_actions 状态机（chat._approve_pending_action / _approve_legacy_handoff
    → executor → Appointment）。原来的 persist=True 直接写 confirmed 预约的分支
    已删除，参数 persist 只为兼容调用方签名保留，不再影响是否落库。

    Args:
        payload : 问诊移交数据包
        persist : 仅用于文案口径（False 时提示"预约信息不会被保存"），不落库

    Returns:
        (确认文案, 结构化预约数据 dict)，结构化数据供 SSE appointment 事件使用。
    """
    result = await mock_create_appointment(payload)
    appointment_no = f"APT{uuid.uuid4().hex[:8].upper()}"

    appointment_data = {
        "appointment_no": appointment_no,
        "department": result.department,
        "doctor_name": result.doctor_name,
        "date": result.date,
        "time_slot": result.time_slot,
        "fee": result.fee,
        "queue_number": result.queue_number,
        "diagnosis": payload.primary_disease,
    }

    # 落库已于 B2 移除：这里只有文案。出现 persist=True 说明有调用点试图绕开状态机。
    if persist:
        logger.warning(
            "handle_handoff 被 persist=True 调用：本函数不再落库，"
            "请改用 clinical_actions 状态机（clinical.create_action → approve）"
        )
    else:
        logger.info("游客模式：跳过挂号记录落库")

    text = (
        f"已为您成功预约挂号！\n\n"
        f"预约号：{appointment_no}\n"
        f"就诊科室：{result.department}\n"
        f"就诊医生：{result.doctor_name}\n"
        f"就诊时间：{result.date} {result.time_slot}\n"
        f"就诊序号：第 {result.queue_number} 号\n"
        f"挂号费：{result.fee} 元\n\n"
        f"请提前 15 分钟到诊室门口等候，祝您早日康复。"
    )
    if not persist:
        text += "\n\n（本次为未登录咨询，预约信息不会被保存，登录后可在“我的预约”中查看和管理。）"
    return text, appointment_data


# 生产环境
# async def handle_handoff(payload: InquiryHandoffPayload) -> str:
#     """
#     接收问诊移交数据包，执行挂号流程。
#     返回给用户的挂号确认消息。
#     """
#     agent = get_inquiry_worker_agent()
#     prompt = INQUIRY_WORKER_PROMPT.format(
#         handoff_payload=payload.model_dump_json(indent=2, ensure_ascii=False)
#     )
#     # TODO 这个 Agent 中有挂号工具
#     result = await agent.ainvoke(
#         {"messages": [{"role": "user", "content": prompt}]}
#     )
#     return result["messages"][-1].content