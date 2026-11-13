import uuid
from typing import Any

from loguru import logger
from pydantic import BaseModel, Field

from src.agents.builder import build_worker_agent, get_runtime_agent
from src.agents.inquiry.state import InquiryHandoffPayload
from src.core.agent_registry import AgentRuntimeConfig, get_active_config
from src.core.config import get_llm
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
- 挂号费主任医师 50 元，其他 30 元"""

    try:
        return await structured_llm.ainvoke(prompt)
    except Exception as e:
        logger.warning(f"预约信息 LLM 生成失败，使用本地兜底数据: {type(e).__name__}: {e}")
        return _fallback_appointment(payload)

# 测试环境用，不依赖真实挂号系统
async def handle_handoff(payload: InquiryHandoffPayload, persist: bool = True) -> tuple[str, dict]:
    """
    接收问诊移交数据包，生成模拟预约结果、落库并返回确认文案。

    Args:
        payload : 问诊移交数据包
        persist : False=游客模式，只生成文案不落库（未登录用户不留预约记录）

    Returns:
        (确认文案, 结构化预约数据 dict)，结构化数据供 SSE appointment 事件使用。
    """
    result = await mock_create_appointment(payload)
    appointment_no = f"APT{uuid.uuid4().hex[:8].upper()}"

    # ── 挂号结果落库（失败仅告警，不阻断文案返回） ──────────────────────
    patient_id: int | None = None
    try:
        patient_id = int(payload.patient_id) if payload.patient_id else None
    except (TypeError, ValueError):
        patient_id = None

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

    if not persist:
        logger.info("游客模式：跳过挂号记录落库")
    else:
        try:
            from src.infra.database import AsyncSessionLocal
            from src.modules.medical.model import Appointment

            async with AsyncSessionLocal() as session:
                session.add(Appointment(
                    patient_id=patient_id,
                    session_id=payload.session_id or None,
                    appointment_no=appointment_no,
                    doctor_name=result.doctor_name,
                    department=result.department,
                    date=result.date,
                    time_slot=result.time_slot,
                    fee=result.fee,
                    queue_number=result.queue_number,
                    status="confirmed",
                    diagnosis=payload.primary_disease or None,
                ))
                await session.commit()
        except Exception as e:
            logger.warning(f"挂号结果落库失败（不影响文案返回）: {e}")

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