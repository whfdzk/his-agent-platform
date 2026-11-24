# src/agents/worker_tools.py
#
# Supervisor 持有的"调用各 worker agent"的工具集。
#
# 归属说明：这些工具直接依赖 agents.inquiry.graph 与各 worker 模块，属于
# agents 层。原先放在 src/tools/ 下会让最底层的 tools 反向依赖上层 agents，
# 且 src/tools/patient_tools.py 为了拿 UserContext 还得连带把整条 agent
# 依赖链拉进来。移到本层后依赖方向是单向的：api → agents → tools → core。

from langchain_core.messages import HumanMessage
from langchain_core.tools import tool
from loguru import logger
from langgraph.prebuilt import ToolRuntime

from src.core.agent_registry import get_active_config
from src.core.concurrency import guarded
from src.core.context import UserContext
from src.core.limits import BudgetExceeded, consume_tool_call
from src.core.permissions import is_staff_role
from src.core.langfuse_tracing import trace_span
from src.agents.inquiry.graph import run_inquiry
from src.agents.inquiry.deps import build_inquiry_deps
from src.agents.workers.report_agent import get_report_agent
from src.agents.workers.drug_agent import get_drug_agent
from src.agents.workers.knowledge_agent import get_knowledge_agent
from src.agents.workers.operation_agent import get_operation_agent
from src.agents.workers.health_agent import get_health_agent
from src.agents.workers.guide_agent import get_guide_agent


from src.agents.inquiry.state import InquiryPhase, InquiryState
from src.agents.memory_middleware import build_memory_block
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
    message = await _with_long_term_memory(runtime, message)

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
        tenant_id = 1
        print(f"⚠️  call_inquiry_agent 未收到 context，从 thread_id 回退解析 user_id={user_id}, session_id={session_id}")
    else:
        session_id = ctx.session_id
        user_id = ctx.user_id
        authenticated = getattr(ctx, "authenticated", True)
        tenant_id = int(getattr(ctx, "tenant_id", 1) or 1)
    blocked = _guard_budget("call_inquiry_agent")
    if blocked:
        return blocked

    try:
        async with trace_span(
            "consultation_agent", "agent", agent_name="consultation",
            attributes={"message_chars": len(message)},
        ):
            return await _run_inquiry(
                message, user_id, session_id, authenticated, tenant_id=tenant_id,
            )
    except Exception as e:
        # 问诊图超时/异常必须在这里降级成一句提示：
        # 否则异常会冒到 supervisor 的整轮 astream，把已经跑完的结论一起判死。
        logger.warning(f"inquiry agent 降级: {type(e).__name__}: {e}")
        return _degrade_message(
            "inquiry", "问诊服务暂时繁忙，请稍后重试；如为紧急情况请及时线下就医。"
        )


async def _create_handoff_action(
    payload, user_id: str, session_id: str, authenticated: bool, tenant_id: int = 1,
) -> int | None:
    """把"待患者确认的挂号意向"落一条 clinical_action，返回 action_id。

    B2 之前这条意向只存在 Redis 里（ex=600）：没有审计留痕，Redis 重启就丢，
    管理端也看不到"谁在什么时候被建议挂号、患者怎么回应"。
    游客不落库（没有 patient_id，也无法确认）；落库失败只告警，不影响把结论返回给用户。
    """
    if not authenticated:
        return None
    try:
        from src.modules.clinical import actions as clinical
        from src.modules.clinical.link import remember_pending
        from src.modules.clinical.model import ACTION_HANDOFF, SOURCE_AGENT

        patient_id = None
        try:
            patient_id = int(payload.patient_id) if payload.patient_id else None
        except (TypeError, ValueError):
            patient_id = None

        thread_id = f"{user_id}:{session_id}"
        async with AsyncSessionLocal() as db:
            row = await clinical.create_action(
                db,
                action_type=ACTION_HANDOFF,
                payload={"handoff_payload": payload.model_dump()},
                summary=f"预约 {payload.department or '待定科室'}｜{payload.primary_disease or '问诊结论'}",
                patient_id=patient_id,
                user_id=user_id,
                session_id=session_id,
                thread_id=thread_id,
                source=SOURCE_AGENT,
                requested_by="agent:consultation",
                tenant_id=tenant_id,
            )
        await remember_pending(thread_id, row.id)
        return row.id
    except Exception as e:  # noqa: BLE001
        logger.warning(f"待确认意向落库失败（不影响结论返回）: {type(e).__name__}: {e}")
        return None


async def _run_inquiry(
    message: str, user_id: str, session_id: str, authenticated: bool,
    *, tenant_id: int = 1,
) -> str:
    """问诊图执行主体：DB 会话短作用域借用，callbacks 保证用量记账不漏。"""
    # 首轮：初始化空状态，执行第一轮问诊; 调用工作流
    # 子图独立预算（WORKER_TIMEOUT < TURN_TIMEOUT）：跑满时只牺牲子图，
    # 保证 supervisor 还有时间做收尾，而不是整轮超时丢结果
    from src.core.llm_callbacks import make_usage_handler
    handler = make_usage_handler(
        user_id, session_id, default_worker="inquiry", tenant_id=tenant_id,
    )

    # 续接已有问诊状态：这里原先硬编码 existing_state=None，
    # 于是从 supervisor 工具入口进来的问诊永远从零开始——
    # 已经聊过"头痛"的会话再补一句"头还是疼"，会被当成全新的模糊描述，
    # 反问"是身体哪个部位不舒服"。Redis 里存着上一轮的 InquiryState，直接续上。
    redis = get_checkpointer_redis()
    state_key = f"inquiry_state:{user_id}:{session_id}"
    raw_state = await redis.get(state_key)
    existing_state: InquiryState | None = None
    if raw_state:
        try:
            existing_state = InquiryState.model_validate_json(raw_state)
        except Exception as e:
            logger.warning(f"inquiry_state 解析失败，按首轮处理: {type(e).__name__}: {e}")

    async with AsyncSessionLocal() as db:
        # 游客模式 persist=False：不写问诊记录，状态只保留在 Redis（会话级短期记忆）
        deps = await build_inquiry_deps(
            db_session=db, persist=authenticated,
            tenant_id=tenant_id,
        )
        reply, new_state = await guarded(
            run_inquiry(
                user_message=message,
                thread_id=f"{user_id}:{session_id}",
                deps=deps,
                existing_state=existing_state,
                user_id=user_id if authenticated else None,
                callbacks=[handler],
                session_id=session_id,
            ),
            timeout=settings.WORKER_TIMEOUT,
        )

    # 本轮已出结论（移交挂号 或 直接结束）：立即解除"问诊进行中"标记。
    # 少写这一支时，首轮就收敛的会诊会把 active 标记留下来，
    # 下一轮用户哪怕只是闲聊也会被强制路由进问诊图。
    if new_state.phase in (InquiryPhase.END, InquiryPhase.HANDOFF):
        # 结束即解除"进行中"标记；有移交数据包时保留状态 10 分钟等患者确认，
        # 但不能在这里直接 handle_handoff —— 那等于替患者做了决定，
        # 话术里还在问"是否需要预约"，后台已经建好了一条挂号记录。
        await redis.delete(f"inquiry_active:{user_id}:{session_id}")
        if new_state.handoff_payload:
            await redis.set(state_key, new_state.model_dump_json(), ex=600)
            # B2：待确认意向同时落 clinical_actions（审计留痕 + 跨进程可恢复），
            # Redis 里只保留指针，供 chat 层收尾时下发确认卡片
            action_id = await _create_handoff_action(
                new_state.handoff_payload, user_id, session_id, authenticated,
                tenant_id=tenant_id,
            )
            logger.info(f"问诊出结论，待患者确认 action_id={action_id} user={user_id}")
            ask = "请问是否现在帮您预约挂号？回复「需要」即可预约，回复「不用」就不预约。"
            tail = "" if "预约" in reply and "？" in reply.split("预约")[-1][:8] else f"\n\n{ask}"
            return f"{reply}{tail}"
        await redis.delete(state_key)
        return reply

    # 问诊未结束：保存状态，设置活跃标记，等待后续轮次
    await redis.set(
        state_key,
        new_state.model_dump_json(),
        ex=3600,  # 1 小时过期
    )

    active_key = f"inquiry_active:{user_id}:{session_id}"
    await redis.set(active_key, "1", ex=3600)

    return reply


def _with_history(runtime: ToolRuntime, message: str, turns: int = 6) -> str:
    """把 supervisor 本线程最近的对话接到 worker 的输入前面。

    worker agent 是"无 checkpointer、每次全新实例"，它只看到工具参数的这一句。
    于是问诊刚说完头痛、用户接着问"需要吃药吗"，用药 agent 会答
    "请详细描述一下您的症状"——因为它根本不知道前文。
    supervisor 的图 state 里有完整 messages，用 ToolRuntime 拿最近几条带过去。
    """
    try:
        state = getattr(runtime, "state", None) or {}
        history = list(state.get("messages") or [])[-turns:]
    except Exception:
        return message
    lines = []
    for m in history:
        content = getattr(m, "content", "")
        if not isinstance(content, str) or not content.strip():
            continue
        role = "患者" if isinstance(m, HumanMessage) else "助手"
        lines.append(f"{role}：{content.strip()[:200]}")
    if not lines:
        return message
    ctx = "\n".join(lines)
    return f"【此前对话，供理解指代用，不要原样复述】\n{ctx}\n\n【本次问题】\n{message}"


async def _with_long_term_memory(runtime: ToolRuntime, message: str) -> str:
    """给 worker 的输入附加该用户的长期记忆。

    worker agent 是独立实例，既没有 supervisor 的 checkpointer，也拿不到
    supervisor 的 middleware 注入。实测：库里记着"我有高血压5年，一直在吃氨氯地平"，
    问"血压高平时要注意什么"却只回通用建议，因为子 agent 根本看不到这条记忆。
    """
    store = getattr(runtime, "store", None)
    user_id, _session_id, _role = _ctx_of(runtime)
    if store is None or not user_id:
        return message
    try:
        block = await build_memory_block(store, user_id, message)
    except Exception as e:  # 记忆增强失败不能拖垮 worker 调用
        logger.warning(f"worker 记忆注入跳过（{type(e).__name__}: {e}）")
        return message
    return f"{message}\n\n{block}" if block else message


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
    tenant_id = int(getattr(runtime.context, "tenant_id", 1) or 1)
    message = _with_history(runtime, message)
    message = await _with_long_term_memory(runtime, message)
    try:
        async with trace_span(
            span_name, "agent", agent_name=agent_name,
            attributes={"message_chars": len(message)},
        ):
            result = await guarded(
                get_agent().ainvoke(
                    {"messages": [{"role": "user", "content": message}]},
                    config={"configurable": {"thread_id": f"{user_id}:{session_id}"}},
                    context=UserContext(
                        user_id=user_id, session_id=session_id, tenant_id=tenant_id,
                        role=_role,
                    ),
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
    # 工具面按角色收敛：患者拿到的是一句拒绝，不进运营 Agent、不起 DB 会话。
    # 工具内部的 is_staff 守卫（knowledge/tools.py: search_knowledge_sql）仍然保留 ——
    # 两道是互补的：这里挡"根本不进链"，那里挡"配置漂移把工具挂错人"。
    _uid, _sid, role = _ctx_of(runtime)
    if not is_staff_role(role):
        logger.info(f"call_operation_agent 被角色拦截：role={role} user={_uid}")
        return "运营数据查询仅对内部运营人员开放，当前账号无权访问。"
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
