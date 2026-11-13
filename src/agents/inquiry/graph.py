# src/agents/inquiry/graph.py
#
# 问诊状态机：只负责路由编排与执行入口。
# 节点实现在 nodes.py，依赖容器在 deps.py。

from __future__ import annotations

from loguru import logger
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import StateGraph, END

from src.agents.inquiry.state import (
    InquiryState, InquiryPhase, InquiryHandoffPayload, PatientContext
)

from src.agents.inquiry.deps import InquiryDeps
from src.agents.inquiry.nodes import (
    node_load_context,
    node_check_emergency,
    node_extract_symptoms,
    node_clarify,
    node_query_neo4j,
    node_ask_symptoms,
    node_parse_answer,
    node_conclude,
    node_save_record,
)

def route_dispatcher(state: InquiryState) -> str:
    """入口分发：首轮走完整流程，后续轮次根据上一轮状态决定入口。"""
    if state.round == 0:
        return "load_context"
    # 上一轮是追问症状（pending_ask_symptoms 非空）→ 解析用户回答
    if state.pending_ask_symptoms:
        return "parse_answer"
    # 上一轮是澄清引导（pending_ask_symptoms 为空）→ 提取新症状
    return "extract_symptoms"


def route_after_emergency(state: InquiryState) -> str:
    """急症检查后的路由：急症直接结束，否则提取症状。"""
    if state.phase == InquiryPhase.END:
        return END
    return "extract_symptoms"


def route_after_extract(state: InquiryState) -> str:
    """症状提取后的路由：有症状查图谱，没症状澄清。"""
    if state.phase == InquiryPhase.GRAPH_QUERY:
        return "query_neo4j"
    return "clarify"


def route_after_neo4j(state: InquiryState) -> str:
    """Neo4j 查询后的路由：收敛则结论，否则追问。"""
    if state.phase == InquiryPhase.CONCLUDE:
        return "conclude"
    return "ask_symptoms"


def route_after_ask(state: InquiryState) -> str:
    """追问后的路由：没有更多症状可问则直接结论。"""
    if state.phase == InquiryPhase.CONCLUDE:
        return "conclude"
    return END  # 等待用户回答（下一轮消息进来后从 parse_answer 继续）


def route_after_parse(state: InquiryState) -> str:
    """解析回答后：重新查 Neo4j 更新候选疾病。"""
    return "query_neo4j"


def route_after_conclude(state: InquiryState) -> str:
    """结论后：保存记录，结束。"""
    if state.phase == InquiryPhase.END:
        return END
    return "save_record"


# ════════════════════════════════════════════════════════════════════════
# 图的组装
# ════════════════════════════════════════════════════════════════════════

def build_inquiry_graph(deps: InquiryDeps):
    """
    构建并编译问诊 StateGraph。
    deps 通过闭包注入到每个节点函数中。
    """
    # 用闭包把 deps 绑定到节点函数
    async def _load_context(state):    return await node_load_context(state, deps)
    async def _check_emergency(state): return await node_check_emergency(state, deps)
    async def _extract_symptoms(state):return await node_extract_symptoms(state, deps)
    async def _clarify(state):         return await node_clarify(state, deps)
    async def _query_neo4j(state):     return await node_query_neo4j(state, deps)
    async def _ask_symptoms(state):    return await node_ask_symptoms(state, deps)
    async def _parse_answer(state):    return await node_parse_answer(state, deps)
    async def _conclude(state):        return await node_conclude(state, deps)
    async def _save_record(state):     return await node_save_record(state, deps)

    graph = StateGraph(InquiryState)

    # 注册节点（dispatcher 是无操作的分发节点，仅用于路由）
    graph.add_node("dispatcher",       lambda state: {})
    graph.add_node("load_context",     _load_context)
    graph.add_node("check_emergency",  _check_emergency)
    graph.add_node("extract_symptoms", _extract_symptoms)
    graph.add_node("clarify",          _clarify)
    graph.add_node("query_neo4j",      _query_neo4j)
    graph.add_node("ask_symptoms",     _ask_symptoms)
    graph.add_node("parse_answer",     _parse_answer)
    graph.add_node("conclude",         _conclude)
    graph.add_node("save_record",      _save_record)

    # 入口：dispatcher 根据 round 分发到首轮流程或后续轮次流程
    graph.set_entry_point("dispatcher")

    # 注册边（固定边）
    graph.add_edge("load_context", "check_emergency")
    graph.add_edge("clarify", END)          # 澄清后等待用户回复
    graph.add_edge("save_record", END)

    # 注册条件边（路由）
    graph.add_conditional_edges("dispatcher",       route_dispatcher,
                                 {"load_context": "load_context", "parse_answer": "parse_answer",
                                  "extract_symptoms": "extract_symptoms"})
    graph.add_conditional_edges("check_emergency",  route_after_emergency,
                                 {"extract_symptoms": "extract_symptoms", END: END})
    graph.add_conditional_edges("extract_symptoms", route_after_extract,
                                 {"query_neo4j": "query_neo4j", "clarify": "clarify"})
    graph.add_conditional_edges("query_neo4j",      route_after_neo4j,
                                 {"conclude": "conclude", "ask_symptoms": "ask_symptoms"})
    graph.add_conditional_edges("ask_symptoms",     route_after_ask,
                                 {"conclude": "conclude", END: END})
    graph.add_conditional_edges("parse_answer",     route_after_parse,
                                 {"query_neo4j": "query_neo4j"})
    graph.add_conditional_edges("conclude",         route_after_conclude,
                                 {"save_record": "save_record", END: END})
    return graph.compile()


# ════════════════════════════════════════════════════════════════════════
# 对外接口：供 call_inquiry_agent 工具调用
# ════════════════════════════════════════════════════════════════════════

def _strip_user_prefix(thread_id: str, session_id: str | None) -> str:
    """从 "user:session" 形式的 thread_id 里还原前端原始 session_id。

    历史数据里 InquiryState.session_id 被写成了 thread_id（带 user 前缀），
    落库后会话列表/历史回放都会多拼一层前缀，这里统一纠正。
    """
    if session_id:
        return session_id
    parts = thread_id.split(":", 1)
    return parts[1] if len(parts) == 2 and parts[1] else thread_id


async def run_inquiry(
    user_message: str,
    thread_id: str,
    deps: InquiryDeps,
    existing_state: InquiryState | None = None,
    user_id: str | None = None,
    long_term_memories: list[str] | None = None,
    callbacks: list | None = None,
    session_id: str | None = None,
) -> tuple[str, InquiryState]:
    """
    执行一轮问诊对话。

    Args:
        user_message      : 用户本轮输入
        thread_id        : thread_id（用于关联 Redis 和 PostgreSQL 记录）
        deps              : 依赖注入容器
        existing_state    : 上一轮的状态（多轮对话时传入）
        user_id           : 来自 UserContext 的用户 ID，贯穿整个问诊流程
        long_term_memories: Supervisor 从 Milvus 检索到的长期记忆摘要
        callbacks         : LangChain 回调（如 token 用量追踪 handler），
                            挂在 graph.ainvoke 的 config 上，自动传播到所有节点内的 LLM 调用
        session_id        : 前端原始会话 ID。必须与 thread_id 区分：
                            写成 thread_id 会把 "user:" 前缀带进 consultations/appointments，
                            前端回放历史时再拼一层前缀就找不到会话

    Returns:
        (assistant_reply, new_state)
    """
    # 说明：不做图级缓存 —— 节点闭包持有 deps（含按请求注入的 db_session），
    # 缓存编译结果会导致 db_session 注入失效。编译开销相对 LLM 调用可忽略。
    graph = build_inquiry_graph(deps)
    raw_session_id = _strip_user_prefix(thread_id, session_id)

    if existing_state is None:
        # 首轮：初始化状态
        # patient_id 期望是 patients.id（数字）；前端传非数字 ID 时置 None，
        # 避免 pydantic 校验报错导致整个问诊流程 500
        try:
            patient_id = int(user_id) if user_id else None
        except (TypeError, ValueError):
            patient_id = None
        state = InquiryState(
            session_id=raw_session_id,
            patient_context=PatientContext(
                patient_id=patient_id,
                long_term_memories=long_term_memories or [],
            ),
        )
    else:
        state = existing_state
        # 兜底纠正历史状态里被污染的 session_id
        if state.session_id != raw_session_id:
            state.session_id = raw_session_id

    # 追加用户消息
    state.messages.append(HumanMessage(content=user_message))

    logger.info("▶ run_inquiry 开始 | session={} round={} entry={} user_id={}",
                thread_id, state.round,
                "load_context" if state.round == 0 else "parse_answer", user_id)

    # 执行图（dispatcher 节点会根据 round 自动路由到正确的入口）
    config = {"configurable": {"thread_id": thread_id}}
    if callbacks:
        # 回调随 config 传播：节点函数内 deps.llm.ainvoke 会自动继承，
        # 因此无需在每个节点里手动传 config
        config["callbacks"] = callbacks
    result = await graph.ainvoke(state, config=config)
    # 如果 LangGraph 返回的是 dict 快照，就转成 InquiryState；如果返回的是原 state 对象，直接用（零拷贝！）
    # 这里有个关键：因为上一步graph.ainvoke， LangGraph 会直接修改你传进去的 state 对象！（或者返回它的快照）
    # 你的代码里做了兼容处理：
    new_state = InquiryState(**result) if isinstance(result, dict) else result

    # 取最后一条 AI 消息作为回复
    reply = ""
    for msg in reversed(new_state.messages):
        if isinstance(msg, AIMessage):
            reply = msg.content
            break

    logger.info("◀ run_inquiry 完成 | session={} phase={} round={} reply_len={}",
                thread_id, new_state.phase, new_state.round, len(reply))
    return reply, new_state
