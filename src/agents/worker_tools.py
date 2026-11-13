# src/agents/worker_tools.py
#
# Supervisor 持有的"调用各 worker agent"的工具集。
#
# 归属说明：这些工具直接依赖 agents.inquiry.graph 与各 worker 模块，属于
# agents 层。原先放在 src/tools/ 下会让最底层的 tools 反向依赖上层 agents，
# 且 src/tools/patient_tools.py 为了拿 UserContext 还得连带把整条 agent
# 依赖链拉进来。移到本层后依赖方向是单向的：api → agents → tools → core。

from langchain_core.tools import tool
from loguru import logger
from langgraph.prebuilt import ToolRuntime

from src.core.agent_registry import get_active_config
from src.core.concurrency import guarded
from src.core.context import UserContext
from src.core.limits import BudgetExceeded, consume_tool_call
from src.core.tracing import trace_span
from src.agents.inquiry.graph import run_inquiry
from src.agents.inquiry.deps import build_inquiry_deps
from src.agents.workers.report_agent import get_report_agent
from src.agents.workers.drug_agent import get_drug_agent
from src.agents.workers.knowledge_agent import get_knowledge_agent
from src.agents.workers.operation_agent import get_operation_agent
from src.agents.workers.health_agent import get_health_agent
from src.agents.workers.guide_agent import get_guide_agent


from src.agents.inquiry.state import InquiryPhase
from src.agents.workers.inquiry_agent import handle_handoff
from src.infra.redis_cache import get_checkpointer_redis
from src.infra.database import AsyncSessionLocal
from src.core.config import get_settings
from src.knowledge.deps import bind_knowledge_deps, build_knowledge_deps

settings = get_settings()


def _guard_budget(tool_name: str) -> str | None:
    """工具调用预算检查；超限时返回给用户的话术，否则 None。"""
    try:
        consume_tool_call(tool_name)
        return None
    except BudgetExceeded as e:
        logger.warning(f"工具调用预算耗尽: {tool_name} kind={e.kind}")
        return e.message


def _degrade_message(agent_code: str, default: str) -> str:
    """降级话术走 Agent 配置（管理端可改），库里没配就用代码内置的默认文案。"""
    try:
        return get_active_config(agent_code).degrade_message or default
    except Exception:  # 配置读取失败绝不能影响降级路径本身
        return default


def _ctx_of(runtime: ToolRuntime) -> tuple[str, str, str]:
    """解析 (user_id, session_id, role)；context 缺失时回退解析 thread_id。"""
    ctx = runtime.context
    if ctx is not None and getattr(ctx, "user_id", None):
        return (
            str(ctx.user_id),
            str(getattr(ctx, "session_id", "") or ""),
            str(getattr(ctx, "role", "patient") or "patient"),
        )
    configurable = runtime.config.get("configurable", {}) if runtime.config else {}
    thread_id = configurable.get("thread_id", "") or ""
    parts = thread_id.split(":") if thread_id else []
    user_id = parts[0] if parts else "unknown_user"
    session_id = parts[1] if len(parts) > 1 else thread_id
    return user_id, session_id, "patient"


async def _invoke_knowledge_worker(
    *,
    tool_name: str,
    message: str,
    runtime: ToolRuntime,
    span_name: str,
    agent_name: str,
    degrade_message: str,
    with_db_session: bool = False,
):
    """知识/用药/运营 Agent 的统一调用：绑定本请求的知识依赖后再执行。

    依赖通过 contextvar 绑定，Agent 保持单例；只有需要 NL2SQL 的场景才开 DB 会话
    （避免长耗时 Agent 期间占着数据库连接，断连时触发连接 terminate 噪音）。
    """
    user_id, _session_id, role = _ctx_of(runtime)

    async def _run():
        if with_db_session:
            async with AsyncSessionLocal() as db:
                deps = await build_knowledge_deps(db_session=db, user_id=user_id, role=role)
                with bind_knowledge_deps(deps):
                    agent = _AGENT_GETTERS[agent_name]()
                    return await guarded(
                        agent.ainvoke({"messages": [{"role": "user", "content": message}]}),
                        timeout=settings.WORKER_TIMEOUT,
                    )
        deps = await build_knowledge_deps(db_session=None, user_id=user_id, role=role)
        with bind_knowledge_deps(deps):
            agent = _AGENT_GETTERS[agent_name]()
            return await guarded(
                agent.ainvoke({"messages": [{"role": "user", "content": message}]}),
                timeout=settings.WORKER_TIMEOUT,
            )

    try:
        async with trace_span(
            span_name, "agent", agent_name=agent_name,
            attributes={"message_chars": len(message)},
        ):
            result = await _run()
        return result["messages"][-1].content
    except Exception as e:
        logger.warning(f"{agent_name} agent 降级: {type(e).__name__}: {e}")
        return _degrade_message(agent_name, degrade_message)


def _agent_getters() -> dict:
    return {
        "knowledge": get_knowledge_agent,
        "drug": get_drug_agent,
        "operation": get_operation_agent,
    }


_AGENT_GETTERS = _agent_getters()


# 仅第一轮会发起调用
@tool
async def call_inquiry_agent(message: str, runtime: ToolRuntime[UserContext]) -> str:
    """
    启动智慧问诊流程。
    适用场景：患者首次描述症状、询问挂哪个科室时。
    后续多轮对话由系统自动路由，无需再次调用此工具。

    Args:
        message    : 患者描述的症状或问诊需求
    """
    # 获取当前会话 ID 和用户 ID
    ctx = runtime.context
    authenticated = True
    if ctx is None:
        # 回退：从 config.thread_id 解析（和 store_tools.py 一致的模式）
        configurable = runtime.config.get("configurable", {})
        thread_id = configurable.get("thread_id", "")
        parts = thread_id.split(":") if thread_id else []
        user_id = parts[0] if len(parts) >= 1 else "unknown_user"
        session_id = parts[1] if len(parts) >= 2 else thread_id
        authenticated = user_id != "anon"
        print(f"⚠️  call_inquiry_agent 未收到 context，从 thread_id 回退解析 user_id={user_id}, session_id={session_id}")
    else:
        session_id = ctx.session_id
        user_id = ctx.user_id
        authenticated = getattr(ctx, "authenticated", True)
    blocked = _guard_budget("call_inquiry_agent")
    if blocked:
        return blocked

    try:
        async with trace_span(
            "consultation_agent", "agent", agent_name="consultation",
            attributes={"message_chars": len(message)},
        ):
            return await _run_inquiry(message, user_id, session_id, authenticated)
    except Exception as e:
        # 问诊图超时/异常必须在这里降级成一句提示：
        # 否则异常会冒到 supervisor 的整轮 astream，把已经跑完的结论一起判死。
        logger.warning(f"inquiry agent 降级: {type(e).__name__}: {e}")
        return _degrade_message(
            "inquiry", "问诊服务暂时繁忙，请稍后重试；如为紧急情况请及时线下就医。"
        )


async def _run_inquiry(message: str, user_id: str, session_id: str, authenticated: bool) -> str:
    """问诊图执行主体：DB 会话短作用域借用，callbacks 保证用量记账不漏。"""
    # 首轮：初始化空状态，执行第一轮问诊; 调用工作流
    # 子图独立预算（WORKER_TIMEOUT < TURN_TIMEOUT）：跑满时只牺牲子图，
    # 保证 supervisor 还有时间做收尾，而不是整轮超时丢结果
    from src.core.llm_callbacks import make_usage_handler
    handler = make_usage_handler(user_id, session_id, default_worker="inquiry")
    async with AsyncSessionLocal() as db:
        # 游客模式 persist=False：不写问诊记录，状态只保留在 Redis（会话级短期记忆）
        deps = await build_inquiry_deps(db_session=db, persist=authenticated)
        reply, new_state = await guarded(
            run_inquiry(
                user_message=message,
                thread_id=f"{user_id}:{session_id}",
                deps=deps,
                existing_state=None,  # 首轮，无历史状态
                user_id=user_id if authenticated else None,
                callbacks=[handler],
                session_id=session_id,
            ),
            timeout=settings.WORKER_TIMEOUT,
        )

    # 首轮即收敛（精确症状/急症等）：触发挂号移交，不设活跃标记
    if new_state.phase == InquiryPhase.END:
        if new_state.handoff_payload:
            handoff_reply, _appointment_data = await handle_handoff(
                new_state.handoff_payload, persist=authenticated,
            )
            return f"{reply}\n\n---\n{handoff_reply}"
        return reply

    # 问诊未结束：保存状态，设置活跃标记，等待后续轮次
    redis = get_checkpointer_redis()
    state_key = f"inquiry_state:{user_id}:{session_id}"
    await redis.set(
        state_key,
        new_state.model_dump_json(),
        ex=3600,  # 1 小时过期
    )

    active_key = f"inquiry_active:{user_id}:{session_id}"
    await redis.set(active_key, "1", ex=3600)

    return reply


async def _invoke_patient_worker(
    *,
    tool_name: str,
    message: str,
    runtime: ToolRuntime,
    span_name: str,
    agent_name: str,
    degrade_message: str,
    get_agent,
):
    """患者侧 Agent 的统一调用：身份靠 thread_id + UserContext 透传给工具。

    与知识类 Agent（依赖走 contextvar）不同，健康/就医/报告工具直接读库，
    只需要 user_id：这里显式传 context，工具就不必再猜 thread_id 格式。
    """
    blocked = _guard_budget(tool_name)
    if blocked:
        return blocked
    user_id, session_id, _role = _ctx_of(runtime)
    try:
        async with trace_span(
            span_name, "agent", agent_name=agent_name,
            attributes={"message_chars": len(message)},
        ):
            result = await guarded(
                get_agent().ainvoke(
                    {"messages": [{"role": "user", "content": message}]},
                    config={"configurable": {"thread_id": f"{user_id}:{session_id}"}},
                    context=UserContext(user_id=user_id, session_id=session_id),
                ),
                timeout=settings.WORKER_TIMEOUT,
            )
        return result["messages"][-1].content
    except Exception as e:
        logger.warning(f"{agent_name} agent 降级: {type(e).__name__}: {e}")
        return _degrade_message(agent_name, degrade_message)


@tool
async def call_report_agent(message: str, runtime: ToolRuntime[UserContext]) -> str:
    """
    调用报告解读Agent，解读患者的检验报告或影像报告。
    适用场景：患者上传了报告需要解读、询问报告中某项指标含义时。
    message: 报告内容描述或患者的具体问题。
    """
    return await _invoke_patient_worker(
        tool_name="call_report_agent",
        message=message,
        runtime=runtime,
        span_name="report_agent",
        agent_name="report",
        degrade_message="报告解读服务暂时繁忙，请稍后重试；如为紧急情况请及时线下就医。",
        get_agent=get_report_agent,
    )


@tool
async def call_drug_agent(message: str, runtime: ToolRuntime[UserContext]) -> str:
    """
    调用药物Agent，进行药物推荐、药物交互检测或处方审查。
    适用场景：询问用什么药、多种药物能否同服、处方是否安全时。
    message: 患者的用药问题或处方信息。
    """
    blocked = _guard_budget("call_drug_agent")
    if blocked:
        return blocked
    return await _invoke_knowledge_worker(
        tool_name="call_drug_agent",
        message=message,
        runtime=runtime,
        span_name="drug_agent",
        agent_name="drug",
        degrade_message="用药咨询服务暂时繁忙，请稍后重试；用药安全相关疑问请咨询药师或医生。",
    )


@tool
async def call_knowledge_agent(message: str, runtime: ToolRuntime[UserContext]) -> str:
    """
    调用知识问答Agent，回答医学知识类问题（会检索知识库文档与医学知识图谱）。
    适用场景：询问疾病知识、治疗方案、医学术语解释、文献检索时。
    message: 患者或医生的医学知识问题。
    """
    blocked = _guard_budget("call_knowledge_agent")
    if blocked:
        return blocked
    return await _invoke_knowledge_worker(
        tool_name="call_knowledge_agent",
        message=message,
        runtime=runtime,
        span_name="knowledge_agent",
        agent_name="knowledge",
        degrade_message="医学知识问答服务暂时繁忙，请稍后重试。",
    )


@tool
async def call_operation_agent(message: str, runtime: ToolRuntime[UserContext]) -> str:
    """
    调用运营数据Agent，查询医院运营统计数据。
    适用场景：运营人员询问就诊量、收入、科室排名等运营数据时。
    message: 运营人员的数据查询需求（自然语言）。
    """
    blocked = _guard_budget("call_operation_agent")
    if blocked:
        return blocked
    return await _invoke_knowledge_worker(
        tool_name="call_operation_agent",
        message=message,
        runtime=runtime,
        span_name="operation_agent",
        agent_name="operation",
        degrade_message="运营数据查询服务暂时繁忙，请稍后重试。",
        with_db_session=True,
    )


@tool
async def call_health_agent(message: str, runtime: ToolRuntime[UserContext]) -> str:
    """
    调用健康管理Agent，记录与跟踪慢病健康指标。
    适用场景：记录血压/血糖/体重、查看指标趋势图、制定健康计划、设置测量或用药提醒、生成周期健康报告时。
    message: 用户的记录需求或健康管理问题（要原样带上数值，如"血压 130/85"）。
    """
    return await _invoke_patient_worker(
        tool_name="call_health_agent",
        message=message,
        runtime=runtime,
        span_name="health_agent",
        agent_name="health",
        degrade_message="健康管理服务暂时繁忙，请稍后重试；指标异常或有不适请及时线下就医。",
        get_agent=get_health_agent,
    )


@tool
async def call_guide_agent(message: str, runtime: ToolRuntime[UserContext]) -> str:
    """
    调用就医辅助Agent，提供就医流程与信息类帮助。
    适用场景：查询科室介绍与医院信息、按症状问挂什么科、询问就诊要带什么、整理就医资料、挂号预约时。
    message: 用户的就医辅助需求。
    """
    return await _invoke_patient_worker(
        tool_name="call_guide_agent",
        message=message,
        runtime=runtime,
        span_name="guide_agent",
        agent_name="guide",
        degrade_message="就医辅助服务暂时繁忙，请稍后重试；急症请直接前往急诊或拨打 120。",
        get_agent=get_guide_agent,
    )


# 所有 Worker 工具列表，供 Supervisor 使用
WORKER_TOOLS = [
    call_inquiry_agent,
    call_report_agent,
    call_drug_agent,
    call_knowledge_agent,
    call_health_agent,
    call_guide_agent,
    call_operation_agent,
]
