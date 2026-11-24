import asyncio
import time
from typing import Any

from langgraph.checkpoint.redis import AsyncRedisSaver
from langchain.agents import create_agent
from loguru import logger

from src.core.config import get_settings
from src.core.concurrency import to_thread
from src.tools.store_tools import save_memory, search_memory
from src.tools.patient_tools import get_my_profile
from src.infra.embedding import get_embeddings
from src.infra.milvus_client import aget_milvus_client_alias
from src.infra.milvus_store import MilvusStore
from src.agents.memory_middleware import LongTermMemoryMiddleware
from src.infra.redis_cache import get_checkpointer_redis
from src.agents.builder import apply_max_turns, llm_for, summarization_middleware
from src.agents.tool_registry import resolve_tools, resolve_worker_tools
from src.agents.worker_tools import WORKER_TOOLS
from src.modules.handoff.middleware import HandoffInterruptMiddleware
from src.agents.repeat_middleware import RepeatGuardMiddleware
from src.tools.handoff_tools import HANDOFF_TOOL_NAME, request_human_handoff
from src.core.agent_registry import AgentRuntimeConfig, get_active_config
from src.core.context import UserContext

# from tools.worker_tools import WORKER_TOOLS

settings = get_settings()

SUPERVISOR_SYSTEM_PROMPT = """你是医疗平台的智能总助手。

## 你的职责

识别用户意图，调用对应的专项助手处理，整合结果后给出清晰、友好的回复。
你自己不直接回答任何健康、医疗、症状相关的问题，必须通过专项助手完成。

## 可调用的专项助手

- call_inquiry_agent：智慧问诊
  适用：用户描述身体不适、症状，需要分诊建议或预约挂号时
  示例："我头疼发烧"、"肚子疼好几天了"、"不知道该挂什么科"、"有点不舒服"

- call_report_agent：报告解读
  适用：用户上传或描述检验报告、影像报告，需要解读关键指标时
  示例："帮我看看这个血常规报告"、"我的CT结果是什么意思"

- call_drug_agent：药品知识服务（MedicationAgent）
  适用：查询药品说明书/成分/禁忌与不良反应、药物相互作用、过敏风险提示、记录与管理用户用药记录时
  示例："布洛芬和阿莫西林能一起吃吗"、"我对青霉素过敏能用什么替代"、"帮我记一下我在吃二甲双胍"

- call_health_agent：慢病与健康指标管理（HealthManageAgent）
  适用：记录血压/血糖/体重、查看指标趋势图、制定健康计划、设置提醒、生成周期健康报告时
  示例："今天血压 130/85 帮我记一下"、"看看我这个月的血糖趋势"、"每天早上提醒我量血压"

- call_guide_agent：就医辅助（GuideAgent）
  适用：查询科室介绍与医院信息、按症状问挂什么科、就诊要带什么、整理就医资料、挂号预约时
  示例："心内科是看什么的"、"我胸痛该挂什么科"、"明天去复诊要带什么"、"帮我挂个骨科"

- call_knowledge_agent：统一医学知识服务（KnowledgeAgent）
  适用：询问医学知识时，包括医学科普、常见疾病、检验指标、药品说明书、医疗指南、就医科室知识，以及知识来源与版本查询
  示例："高血压平时要注意什么"、"糖尿病的早期症状有哪些"、"这些说法出自哪份指南"

- call_operation_agent：运营数据查询（仅限内部运营人员）
  适用：查询医院就诊量、科室排名、收入统计等运营数据时

## 易混淆场景的取舍（按顺序判断）

1. 用户在描述身体不适/症状，顺带问"该挂什么科"：走 call_inquiry_agent（问诊分诊），不要走 guide
2. 只是问科室介绍、医院信息、就诊流程、挂号：走 call_guide_agent
3. 用户报出具体数值要"记下来"：走 call_health_agent；不要自己复述一遍就当已记录
4. 问的是"要不要吃这个药/两种药能否同服/有什么副作用"：走 call_drug_agent（不要走知识问答）
5. 问的是疾病知识、检验指标含义、指南依据、知识来源：走 call_knowledge_agent

## 记忆工具

- search_memory：在调用专项助手前，先检索用户的历史记忆（过敏史、既往病史、用药偏好等），将相关信息附加到调用参数中，避免重复询问用户
- save_memory：当用户提到重要的个人健康信息时（过敏史、慢性病、长期用药等），主动保存到长期记忆
- get_my_profile：查询当前登录患者的个人档案（姓名、性别、年龄、血型、过敏史、既往病史、脱敏联系方式）

## 转人工（request_human_handoff）

出现下列情况必须调用 request_human_handoff，把对话交给真人坐席，不要自己假装"已转接"：
1. 患者明确要求：说"转人工""找真人""人工客服""有没有人看看""我要投诉"；
2. 同一个问题你已经给过建议，患者再次表示"没解决/还是不行/你说的没用"；
3. 需要人工确认才敢执行的事：具体处方或剂量调整、退费、纠纷、需要查阅线下资料的追问；
4. 患者情绪明显激动，或反复表达不信任。

调用时 summary 用一句话写患者的诉求，不要贴整段对话。调用后本轮会被挂起直到坐席处理，
坐席回复后你会收到坐席原话：此时只用 1-2 句话确认患者已看到并提出后续建议，
不要复述坐席原话，也不要再说"我帮您转接"。

## 身份类问题（"我是谁"、"我的资料/我的病历"、"我对什么过敏"）

严格按以下顺序处理，不要跳步：
1. 先调用 search_memory 检索长期记忆；
2. 若长期记忆里**没有**可用的身份信息（返回"没有找到相关记忆"、"当前为未登录"等），再调用 get_my_profile 从数据库查询当前登录患者的档案作为兜底；
3. 若长期记忆里有信息，直接结合 get_my_profile 返回的档案回答（档案中的姓名/性别/年龄以数据库为准）；
4. get_my_profile 返回"未登录"提示时，如实告知用户当前是游客状态、登录后可查看个人档案，不要编造任何姓名或病史。

## 游客（未登录）模式

1. 未登录用户同样可以正常咨询、问诊、解读报告，你必须正常提供服务，不要拒绝回答。
2. 游客不会保存任何历史记录：不要调用 save_memory（会返回不可用），也不要声称"已为您记录/保存"。
3. 涉及"我的预约""查看历史记录""新建会话"等需要账号的功能时，提示用户登录后使用。
4. 游客的对话只在当前会话内有效（约 1 小时），如需长期保存请引导登录。

## 工作原则

1. 你绝不直接回答健康相关问题。只要用户提到身体不适、症状、不舒服（如"不舒服"、"不太好"、"有点难受"），无论多模糊，都必须调用 call_inquiry_agent。意图不明确时也默认调 call_inquiry_agent。
2. 传递消息规则：调用专项助手时，message 参数必须原样传递用户的原始输入，禁止改写、补充指令或添油加醋。如果 search_memory 检索到了相关历史信息，以"[长期记忆] xxx"的格式附加在用户原文之后，不要修改用户原文本身。
3. 复杂问题可串联：例如用户描述症状后又问用药，先调用 call_inquiry_agent 再调用 call_drug_agent
4. 患者安全优先：识别到"胸痛"、"呼吸困难"、"意识不清"等急症关键词时，立即提示用户拨打 120 或前往急诊，不要等待问诊流程
5. 语气温和专业：用患者能理解的语言表达，避免过度使用医学术语
6. 用户如果打招呼，或者闲聊，则不用调用任何专项助手，直接回答用户问题"""


# 模块级 checkpointer 单例（供 session 历史 API 复用）
_supervisor_checkpointer = None
# checkpointer 独立锁：不能复用 _supervisor_lock，
# 否则 get_supervisor_agent 持锁装配期间 →
# get_supervisor_checkpointer 再次抢同一把非重入锁，会永久死锁
_checkpointer_lock = asyncio.Lock()


# 创建出 监督 Agent
async def get_supervisor_checkpointer():
    """返回全局单例 AsyncRedisSaver checkpointer（并发安全），供 session 历史 API 复用。"""
    global _supervisor_checkpointer
    if _supervisor_checkpointer is None:
        async with _checkpointer_lock:
            if _supervisor_checkpointer is None:
                redis_client = get_checkpointer_redis()
                cp = AsyncRedisSaver(redis_client=redis_client)
                await cp.asetup()
                _supervisor_checkpointer = cp
    return _supervisor_checkpointer


# 长期记忆降级状态：Milvus 不可用时快速失败，避免每次请求都白等 10s 连接超时
_MILVUS_RETRY_INTERVAL = 60.0  # 秒
_milvus_retry_at = 0.0


async def _get_long_term_store():
    """构建长期记忆 Milvus Store；Milvus 不可用时返回 None（降级为无长期记忆）。

    长期记忆属于增强能力：Milvus 挂了不应该让整个聊天不可用，
    否则一次基础设施故障会直接表现为"服务暂时不可用"。
    """
    global _milvus_retry_at
    if time.monotonic() < _milvus_retry_at:
        return None
    try:
        milvus_alias = await aget_milvus_client_alias()
        store = await to_thread(
            lambda: MilvusStore(
                alias=milvus_alias,
                embeddings=get_embeddings(),
                dims=1024,  # Qwen3-Embedding 输出 1024 维
            )
        )
        _milvus_retry_at = 0.0
        return store
    except Exception as e:
        _milvus_retry_at = time.monotonic() + _MILVUS_RETRY_INTERVAL
        logger.warning(
            f"Milvus 不可用，长期记忆降级（{_MILVUS_RETRY_INTERVAL:.0f}s 后重试）: {type(e).__name__}: {e}"
        )
        return None


_AGENT_CODE = "supervisor"
_DEFAULT_TEMPERATURE = 0.1


def _default_memory_tools() -> list[Any]:
    """代码内置默认工具：长期记忆 + 患者档案（worker 调度工具由 sub_agent_codes 决定）。"""
    return [save_memory, search_memory, get_my_profile]


async def _build_supervisor(cfg: AgentRuntimeConfig):
    """按配置装配 Supervisor。

    可配置项：system_prompt / prompt_id / 模型与温度 / 记忆工具清单 /
    可调度的子 Agent(sub_agent_codes) / 会话压缩阈值 / max_turns。
    基础设施（checkpointer、长期记忆 store）仍由代码托管。
    """
    # 1. 复用项目已有的 checkpointer 专用 Redis 客户端（bytes 模式）
    checkpointer = await get_supervisor_checkpointer()

    # 2. 长期记忆（Milvus 不可用时为 None，工具内部会返回降级提示）
    store = await _get_long_term_store()

    # 3. 记忆/档案工具 + 允许调度的 worker 工具
    memory_tools = resolve_tools(cfg.tool_ids, fallback=_default_memory_tools())
    worker_tools = resolve_worker_tools(cfg.sub_agent_codes, fallback=WORKER_TOOLS)

    # 4. 创建 Agent
    # model 显式传入 ChatOpenAI 实例（不是模型名字符串）：
    # - api_key / base_url 由配置（model_configs 或 .env）显式控制，不靠隐式推断
    # - 温度调低：意图路由要稳定、可复现，不适合高温度发散
    # 默认模型跟随 .env 的 CHAT_MODEL（当前网关是 DashScope + qwen-max）。
    # 此前用的是 DEEPSEEK_MODEL，agent 配置里 model 为空时会请求 deepseek-chat，
    # 打到通义网关必然 404（模型不存在），整轮对话直接降级成"服务暂时不可用"。
    llm = llm_for(
        cfg, default_temperature=_DEFAULT_TEMPERATURE, default_model=settings.CHAT_MODEL,
    )

    # 转人工工具常驻：不依赖管理端在工具清单里勾选它。
    # 它是"患者喊人工"的唯一落单入口，漏配等于这个能力直接消失。
    tools = memory_tools + worker_tools
    if HANDOFF_TOOL_NAME not in {getattr(t, "name", "") for t in tools}:
        tools = tools + [request_human_handoff]

    middleware = []
    # 转人工挂起：模型调用 request_human_handoff 时建单并 interrupt()，
    # 坐席处理完由 Command(resume=...) 续跑（见 modules/handoff/middleware.py）
    middleware.append(HandoffInterruptMiddleware())
    # 打转拦截：同一「工具+参数」在窗口内重复提交时，不再执行工具、直接回提示。
    # 放在 handoff 之后：转人工调用本身也可能重复，但那条路径由 handoff 处理更合适
    # （它需要建单/挂起，不能被就地拦成一条文本）。
    middleware.append(RepeatGuardMiddleware())
    summarization = summarization_middleware(cfg, default_model=settings.CHAT_MODEL)
    if summarization is not None:  # 会话总结压缩
        middleware.append(summarization)
    # 长期记忆自动注入：prompt 里虽写了"调专项助手前先 search_memory"，
    # 但真实链路里模型从不执行（日志：保存 2 次 / 搜索 0 次），改为代码强制注入
    middleware.append(LongTermMemoryMiddleware(store))

    agent = create_agent(
        model=llm,
        tools=tools,
        system_prompt=cfg.system_prompt or SUPERVISOR_SYSTEM_PROMPT,
        middleware=middleware,
        checkpointer=checkpointer,  # 短期记忆. agent chat ui（禁用你配置 checkpointer）
        store=store  # 长期记忆. 把同一个用户，任意会话的有价值信息进行存储。
    )
    logger.info(
        f"supervisor 已装配：工具 {len(memory_tools)} 个 + 子 Agent {len(worker_tools)} 个"
        f"（含转人工），提示词来源 {'数据库' if cfg.system_prompt else '代码内置'}，"
        f"会话压缩 {'开启' if summarization else '关闭'}"
    )
    return apply_max_turns(agent, cfg)


async def create_supervisor_agent():
    """按当前配置新建一个实例（不写缓存，便于测试/排查）。"""
    return await _build_supervisor(get_active_config(_AGENT_CODE))


# 模块级单例：避免每次请求都重新创建 agent 和 checkpointer。
# 缓存以「配置对象相等」为失效条件：管理端保存/发布后刷新配置，下次取用时自动重建。
_supervisor_cache: tuple[AgentRuntimeConfig, Any] | None = None
# 并发建链时只允许一个协程执行初始化，避免重复建 checkpointer/索引
_supervisor_lock = asyncio.Lock()


async def get_supervisor_agent():
    """返回全局单例 Agent；配置变化时重建（并发安全）。"""
    global _supervisor_cache
    cfg = get_active_config(_AGENT_CODE)
    cached = _supervisor_cache
    if cached is not None and cached[0] == cfg:
        return cached[1]

    async with _supervisor_lock:
        cfg = get_active_config(_AGENT_CODE)  # 拿锁期间配置可能已刷新
        cached = _supervisor_cache
        if cached is not None and cached[0] == cfg:
            return cached[1]
        agent = await _build_supervisor(cfg)
        _supervisor_cache = (cfg, agent)
        return agent


def reset_supervisor_agent() -> None:
    """仅测试用：丢弃已装配的单例。"""
    global _supervisor_cache
    _supervisor_cache = None


# FastAPI 路由中使用
async def chat_endpoint(user_id: str, session_id: str, message: str):
    # 使用单例，不重复初始化。获取 agent
    agent = await get_supervisor_agent()

    # thread_id 用来区分不同会话。  用户id:会话id:日期（前端传来一个会话id）
    config = {"configurable": {"thread_id": f"{user_id}:{session_id}"}}

    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": message}]},
        config=config,
        context=UserContext(user_id=user_id, session_id=session_id),
    )
    return result["messages"][-1].content
