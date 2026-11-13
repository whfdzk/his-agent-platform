# 读取环境变量
from pathlib import Path

import httpx
from langchain_core.outputs import ChatResult
from langchain_openai import ChatOpenAI
from loguru import logger
from openai import AsyncOpenAI
from pydantic import model_validator
from pydantic_settings import BaseSettings
from functools import lru_cache

from src.core.exceptions import BizErrorCode, BizException
from src.core.model_registry import (
    ModelCandidate,
    cached_candidates,
    get_candidates,
)

# 项目根目录：src/core/config.py 上溯两层
# .env 固定从项目根目录读取，避免因启动时的工作目录不同而读不到配置
BASE_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    APP_NAME: str = "his-agent"
    APP_ENV: str = "dev"
    APP_DEBUG: bool = True

    @model_validator(mode="after")
    def _require_secrets_in_production(self):
        """生产环境必须显式配置签名密钥。

        JWT_SECRET_KEY 为空时 jwt_utils 会回退到硬编码的 dev 密钥，
        上线后等于任何人都能伪造 token，因此宁可启动失败。
        """
        if self.APP_ENV != "dev" and not self.JWT_SECRET_KEY:
            raise ValueError(
                f"APP_ENV={self.APP_ENV} 时必须配置 JWT_SECRET_KEY，否则 JWT 会回退到不安全的默认密钥"
            )
        return self

    DB_HOST: str = "localhost"
    DB_PORT: int = 5432
    DB_USER: str = "postgres"
    DB_PASSWORD: str = "postgres"
    DB_NAME: str = "medical_db"

    # Redis
    REDIS_HOST: str = "localhost"
    REDIS_PORT: int = 6379
    REDIS_PASSWORD: str = ""
    REDIS_DB: int = 0

    # RustFS（S3 兼容对象存储，替代 MinIO）
    # ENDPOINT 可写完整 URL（http://localhost:9000），也可只写 host:port
    RUSTFS_ENDPOINT: str = "http://localhost:9000"
    RUSTFS_ACCESS_KEY: str = "rustfsadmin"
    RUSTFS_SECRET_KEY: str = "rustfsadmin"
    RUSTFS_BUCKET: str = "knowledge-docs"
    RUSTFS_SECURE: bool = False
    RUSTFS_REGION: str = "us-east-1"

    # Milvus
    MILVUS_HOST: str = "localhost"
    MILVUS_PORT: int = 19530

    # Neo4j
    NEO4J_URI: str = "bolt://localhost:7687"
    NEO4J_USER: str = "neo4j"
    NEO4J_PASSWORD: str = "medical123"

    # MinerU（文档解析服务，供知识库导入使用）
    MINERU_API_URL: str = "http://localhost:8888"
    MINERU_BACKEND: str = "pipeline"
    MINERU_TIMEOUT: int = 120  # 解析任务超时（秒）

    # 模型
    OPENAI_API_KEY: str = ""
    DASHSCOPE_API_KEY: str = ""
    EMBEDDING_API_KEY: str = ""  # SiliconFlow（硅基流动），用于 Qwen3 embedding
       # 聊天模型
    BASE_URL_CHAT: str = ""
    DEEPSEEK_API_KEY: str = ""
    DEEPSEEK_MODEL: str = "deepseek-chat"
    CHAT_MODEL: str = "deepseek-chat"
    EMBEDDING_MODEL: str = "Qwen/Qwen3-Embedding-0.6B"
    # 向量服务地址：此前硬编码在 3 个业务模块 + 3 个脚本里，换供应商要改 6 处
    EMBEDDING_BASE_URL: str = "https://api.siliconflow.cn/v1"
    RERANK_MODEL: str = "qwen3-rerank"
    VL_MODEL: str = "Qwen/Qwen3-VL-32B-Instruct"
    # 图片报告 OCR 兜底：MinerU 未部署时用视觉模型把图片报告转录成文本。
    # 此前 VL_MODEL 只定义未接线，jpg/png 报告在 MinerU 不可用时拿到空文本，
    # 指标提取必然为空，前端表现为"未识别到结构化指标"。
    VL_OCR_ENABLED: bool = True       # 关掉后图片只走 MinerU
    VL_OCR_TIMEOUT: float = 90.0      # 单张图片识别超时（秒），图片比文本慢，不能复用 LLM_TIMEOUT
    VL_OCR_MAX_SIDE: int = 1600       # 上传前把长边压到该像素，控制 token 与耗时

    # LLM 请求参数
    LLM_TIMEOUT: float = 60.0  # 单次请求超时（秒）
    LLM_MAX_RETRIES: int = 2  # 请求失败自动重试次数
    LLM_MAX_CONCURRENCY: int = 200  # 单进程 LLM 并发上限（2 worker × 200 = 400）
    TURN_TIMEOUT: float = 240.0  # 单轮对话（整条 agent/图链路）总超时（秒）
    # 取值下限由「单次 LLM 调用的最坏耗时」决定：
    # 单次最坏 = LLM_TIMEOUT × (LLM_MAX_RETRIES + 1) = 60 × 3 = 180s
    # （实测网关抖动时结论节点单个 ainvoke 跑到 107s）。
    # 一轮 supervisor = 路由 LLM + 子 agent + 收尾 LLM，其中子 agent 由
    # WORKER_TIMEOUT 单独封顶，故 240s 能兜住单次抖动，又不至于让挂死的链路长期占连接。
    WORKER_TIMEOUT: float = 150.0  # 单个 worker 子 agent 的独立预算（秒）
    # 模型故障转移：主模型调用失败时按管理端配置的候选链自动切换。
    # 关掉后 get_llm 仍返回 FallbackChatModel，但链里只有 .env 的主模型（等同于旧行为）。
    MODEL_FAILOVER_ENABLED: bool = True
    # 必须明显小于 TURN_TIMEOUT：此前子图与整轮同为 150s，子图一旦跑满就把整轮预算吃光，
    # supervisor 收尾 LLM 无时间可用，已跑完的问诊结论被整轮超时一起丢弃。
    SSE_MAX_CONNS: int = 4000  # 单进程 SSE 并发连接上限（2 worker = 8000）
    DEGRADE_MODE: bool = False  # 降级开关：True 时拒绝新对话轮次，仅返回繁忙提示

    # 配置热加载：管理端写操作会主动刷新本进程；轮询兜住其他副本改了库的情况。
    # 取值只看「可接受的最大滞后」——太大则多副本间行为漂移，太小则空转读库。
    # 单实例部署可以设为 0 关掉轮询，只靠管理端主动推送。
    AGENT_CONFIG_REFRESH_SECONDS: float = 30.0

    # 检索参数（RAG / NL2SQL）：此前散落在各模块里作为魔法值，调参无统一入口
    RETRIEVAL_TOP_K: int = 20          # 向量粗检索条数
    RERANK_TOP_K: int = 5              # 精排后保留条数
    MILVUS_NPROBE: int = 16            # Milvus 检索精度（越大越准越慢）
    GRAPH_RAG_RECORD_LIMIT: int = 20   # 图谱 RAG 单条 Cypher 取回上限
    NL2SQL_MAX_RETRIES: int = 2        # SQL 重试次数
    NL2SQL_TIMEOUT_SECONDS: int = 10   # 单条 SQL 执行超时（秒）
    NL2SQL_ROW_LIMIT: int = 100        # SQL 返回行数上限
    FUSION_SNIPPET_LIMIT: int = 1000   # 融合阶段每条通道送入证据的截断长度
    DOC_LIST_LIMIT: int = 500          # 知识库文档列表返回上限
    # 分页：此前各路由各写各的魔法值（200/500/2000/20000），统一口径
    PAGE_SIZE_DEFAULT: int = 50
    PAGE_SIZE_MAX: int = 500

    # 安全
    JWT_SECRET_KEY: str = ""  # 为空时回退到 jwt_utils 旧值（兼容开发环境，生产必须配置）
    # access token 有效期：登出后残留凭证的暴露窗口约等于该值，生产建议 15~30 分钟
    ACCESS_TOKEN_TTL_MINUTES: int = 30
    # 撤销记录（登出全部设备）的保留天数：低于该窗口的旧 token 已自然过期
    AUTH_REVOCATION_TTL_DAYS: int = 90
    # Redis 不可用时管理端是否拒绝放行（fail-closed）。生产建议 true；
    # 默认 false 避免 Redis 抖动把整个管理控制台锁死
    AUTH_ADMIN_FAIL_CLOSED: bool = False
    # /metrics 抓取令牌：Prometheus 带 X-Metrics-Token 头；未配置且非 dev 环境将拒绝访问
    METRICS_TOKEN: str = ""
    CORS_ORIGINS: str = "http://localhost:3000,http://localhost:5173,http://localhost:5174"

    # 单轮执行限额（防跑飞 / 防刷）
    AGENT_MAX_CALLS: int = 12           # 单轮最多 Agent 调用次数
    TOOL_MAX_CALLS: int = 20            # 单轮最多工具调用次数
    TURN_MAX_TOKENS: int = 60000        # 单轮 Token 上限（prompt + completion）
    RETRIEVAL_MAX_RESULTS: int = 8      # 单次检索最多返回条数（预算硬上限）
    USER_MAX_CONCURRENT_TASKS: int = 2  # 同一用户并发任务上限（跨进程，Redis 计数）

    # Safety Engine
    SAFETY_ENABLED: bool = True                 # 输入/输出安全审核总开关
    SAFETY_REVIEW_REQUIRED_LEVEL: str = "high"  # 达到该等级自动转人工复核
    SAFETY_BLOCK_ON_CRITICAL: bool = True       # critical 危险信号直接拦截并给出急救指引
    SAFETY_MAX_INPUT_CHARS: int = 4000          # 单条输入长度上限（防超长注入）

    # 可观测性 / 审计保留周期
    TRACE_RETENTION_DAYS: int = 180        # Agent 轨迹保留天数
    SAFETY_EVENT_RETENTION_DAYS: int = 365 # 安全事件保留天数（合规要求更长）
    ACCESS_LOG_RETENTION_DAYS: int = 30    # 访问日志保留天数
    TRACE_SAMPLE_RATE: float = 1.0         # 轨迹采样率（高并发时可下调）

    # 可观测
    SENTRY_DSN: str = ""  # 为空则不启用 Sentry

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    LOG_LEVEL: str = "DEBUG"
    LOG_DIR: str = "logs"

    @property
    def DATABASE_URL(self) -> str:
        return (
            f"postgresql+asyncpg://{self.DB_USER}:{self.DB_PASSWORD}"
            f"@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"
        )

    # 指定环境变量文件（固定指向项目根目录的 .env）
    model_config = {
        "env_file": str(BASE_DIR / ".env"),
        "env_file_encoding": "utf-8",
    }

# 保存到内存缓存中。以后直接获取。
@lru_cache  # lru 把对象实例保存到内存中。这是一种单例的实现
def get_settings() -> Settings:
    return Settings()

@lru_cache  # 复用连接池，避免每次创建新客户端
def _bypass_proxy_http_clients() -> tuple[httpx.Client, httpx.AsyncClient]:
    """显式禁用系统代理（HTTP(S)_PROXY / ALL_PROXY）的 httpx 客户端。

    httpx 默认 trust_env=True 会读取代理环境变量；本机代理工具（端口随机）
    重启或未启动时会导致 LLM 请求间歇性 ConnectError。
    国内网关（硅基流动/DeepSeek 等）可直连，无需代理。

    10k 并发必须显式调大连接池上限：httpx 默认 max_connections=100，
    会把全部 LLM 请求硬封顶在 100 并发。
    """
    limits = httpx.Limits(
        max_connections=400,
        max_keepalive_connections=100,
        keepalive_expiry=60.0,
    )
    timeout = httpx.Timeout(60.0, connect=10.0)
    return (
        httpx.Client(trust_env=False, limits=limits, timeout=timeout),
        httpx.AsyncClient(trust_env=False, limits=limits, timeout=timeout),
    )


def _resolve_chat_credentials() -> tuple[str, str]:
    """统一 API Key 优先级与 base_url 归一化。

    此前 api.py 的 _build_client() 用 OPENAI>DEEPSEEK>DASHSCOPE，与这里的
    DEEPSEEK>OPENAI>DASHSCOPE 相反，同一份 .env 在不同代码路径会选出不同厂商。
    现在两条路径共用这一个解析器。
    """
    settings = get_settings()

    api_key = settings.DEEPSEEK_API_KEY or settings.OPENAI_API_KEY or settings.DASHSCOPE_API_KEY
    if not api_key:
        raise BizException(BizErrorCode.CONFIG_MISSING,
            "未配置任何 API Key，请在 .env 中设置 DEEPSEEK_API_KEY / OPENAI_API_KEY / DASHSCOPE_API_KEY"
        )

    return api_key, _normalize_base_url(settings.BASE_URL_CHAT)


def _normalize_base_url(base_url: str) -> str:
    """容忍把 base_url 误配成完整的 /chat/completions 地址。"""
    if base_url and base_url.endswith("/chat/completions"):
        return base_url.rsplit("/chat/completions", 1)[0]
    return base_url


# ── 故障转移：候选模型 ──────────────────────────────────────────────────
# 同一个候选可能被多个 agent 复用（supervisor / inquiry / drug ...），
# 每个 (候选, temperature) 只建一个客户端，避免每次调用都新建 httpx 连接。
_fallback_clients: dict[tuple, ChatOpenAI] = {}


def _build_candidate_client(candidate: ModelCandidate, temperature: float) -> ChatOpenAI:
    s = get_settings()
    # 候选未单独配 key/base_url 时沿用 .env 的凭证（只换模型名是常见用法）
    api_key = candidate.api_key or _resolve_chat_credentials()[0]
    sync_client, async_client = _bypass_proxy_http_clients()
    return ChatOpenAI(
        model=candidate.model_name,
        api_key=api_key,
        base_url=_normalize_base_url(candidate.base_url) or None,
        temperature=temperature,
        timeout=s.LLM_TIMEOUT,
        max_retries=s.LLM_MAX_RETRIES,
        http_client=sync_client,
        http_async_client=async_client,
    )


class FallbackChatModel(ChatOpenAI):
    """主模型调用失败时自动切换到候选模型的 ChatOpenAI。

    候选链来自 core/model_registry（modules 层从 model_configs 注入），
    每次调用前重新读取，管理端改配置无需重启；链为空时就是普通 ChatOpenAI。

    调用顺序：DB 里配的候选链（is_default 最前，其余按 priority 升序）→
    最后兜底 .env 的主模型（self），保证 DB 全挂时平台仍能出结果。

    设计取舍：
    - 不改用 RunnableWithFallbacks —— create_agent 需要对模型 bind_tools，
      RunnableWithFallbacks 没有 bind_tools，会把整个 agent 打挂。
    - 流式只在"还没吐出第一个 chunk"时才切换：已经输出过内容再重试会重复正文。
    """

    def _candidate_clients(self) -> list[ChatOpenAI]:
        clients: list[ChatOpenAI] = []
        for c in cached_candidates():
            key = (c.name, c.model_name, c.base_url, c.api_key, self.temperature)
            # 不能用 setdefault(k, build())：默认值是"先求值再判断"，
            # 每次调用都会白建一个 httpx 客户端（并发下还会重复建连接池）
            client = _fallback_clients.get(key)
            if client is None:
                client = _build_candidate_client(c, self.temperature)
                _fallback_clients[key] = client
            clients.append(client)
        return clients

    async def _resolve_chain(self) -> list[ChatOpenAI]:
        if not get_settings().MODEL_FAILOVER_ENABLED:
            return [self]
        candidates = await get_candidates()
        return self._candidate_clients() + [self]

    def _chain_sync(self) -> list[ChatOpenAI]:
        if not get_settings().MODEL_FAILOVER_ENABLED:
            return [self]
        return self._candidate_clients() + [self]

    def _note_switch(self, idx: int, total: int, err: Exception) -> None:
        logger.warning(
            "LLM 调用失败，切换候选模型 {}/{}: {}: {}",
            idx + 1, total - 1, type(err).__name__, err,
        )

    # ── 非流式 ──────────────────────────────────────────────────────────
    async def _agenerate(
        self, messages, stop=None, run_manager=None, **kwargs,
    ) -> ChatResult:
        chain = await self._resolve_chain()
        last: Exception | None = None
        for idx, client in enumerate(chain):
            try:
                if client is self:
                    return await ChatOpenAI._agenerate(
                        self, messages, stop=stop, run_manager=run_manager, **kwargs
                    )
                return await client._agenerate(
                    messages, stop=stop, run_manager=run_manager, **kwargs
                )
            except Exception as e:
                last = e
                if idx + 1 < len(chain):
                    self._note_switch(idx, len(chain), e)
        raise last  # type: ignore[misc]

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        chain = self._chain_sync()
        last: Exception | None = None
        for idx, client in enumerate(chain):
            try:
                if client is self:
                    return ChatOpenAI._generate(
                        self, messages, stop=stop, run_manager=run_manager, **kwargs
                    )
                return client._generate(
                    messages, stop=stop, run_manager=run_manager, **kwargs
                )
            except Exception as e:
                last = e
                if idx + 1 < len(chain):
                    self._note_switch(idx, len(chain), e)
        raise last  # type: ignore[misc]

    # ── 流式 ────────────────────────────────────────────────────────────
    async def _astream(self, *args, **kwargs):
        chain = await self._resolve_chain()
        last: Exception | None = None
        for idx, client in enumerate(chain):
            emitted = False
            agen = (
                ChatOpenAI._astream(self, *args, **kwargs)
                if client is self
                else client._astream(*args, **kwargs)
            )
            try:
                async for chunk in agen:
                    emitted = True
                    yield chunk
                return
            except Exception as e:
                last = e
                # 已经吐过内容就无法安全重放，直接上抛
                if emitted or idx + 1 >= len(chain):
                    raise
                self._note_switch(idx, len(chain), e)
        raise last  # type: ignore[misc]

    def _stream(self, *args, **kwargs):
        chain = self._chain_sync()
        last: Exception | None = None
        for idx, client in enumerate(chain):
            emitted = False
            it = (
                ChatOpenAI._stream(self, *args, **kwargs)
                if client is self
                else client._stream(*args, **kwargs)
            )
            try:
                for chunk in it:
                    emitted = True
                    yield chunk
                return
            except Exception as e:
                last = e
                if emitted or idx + 1 >= len(chain):
                    raise
                self._note_switch(idx, len(chain), e)
        raise last  # type: ignore[misc]


@lru_cache(maxsize=64)
def get_llm(temperature: float = 0.7, model: str | None = None) -> ChatOpenAI:
    """获取 OpenAI 兼容的聊天模型客户端（DeepSeek / 通义 / 自建网关均可）。

    返回的是 FallbackChatModel：默认走 .env 的主模型，失败时按管理端配置的
    候选链（model_configs 里 model_type=chat、按 priority 排序）自动切换。

    按 (temperature, model) 缓存复用：客户端本身无状态，重复构造只是浪费对象，
    真正的连接池由 _bypass_proxy_http_clients() 统一持有。

    - api_key 按优先级取 DEEPSEEK_API_KEY / OPENAI_API_KEY / DASHSCOPE_API_KEY，缺失时启动即报错
    - base_url 优先用 BASE_URL_CHAT（自建网关），并容忍误配成完整 /chat/completions 地址
    - 显式传入 api_key / base_url / 超时 / 重试，不依赖进程环境变量的隐式推断
    - httpx 客户端 trust_env=False，绕过本机代理，避免代理进程抖动引发 ConnectError
    """
    s = get_settings()
    api_key, base_url = _resolve_chat_credentials()
    sync_client, async_client = _bypass_proxy_http_clients()

    return FallbackChatModel(
        model=model or s.CHAT_MODEL,
        api_key=api_key,
        base_url=base_url or None,
        temperature=temperature,
        timeout=s.LLM_TIMEOUT,
        max_retries=s.LLM_MAX_RETRIES,
        http_client=sync_client,
        http_async_client=async_client,
    )


def build_llm(
    temperature: float = 0.7,
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
) -> ChatOpenAI:
    """按管理端配置构造 LLM 客户端（Agent 配置里的模型 / 温度 / 网关）。

    与 get_llm 的区别：这里允许显式传入 base_url / api_key（来自 model_configs 表），
    且不做 lru_cache —— Agent 配置可以随时改，缓存留给上层按配置指纹判断。
    仍然是 FallbackChatModel，保留故障转移能力。
    """
    s = get_settings()
    env_key, env_url = _resolve_chat_credentials()
    sync_client, async_client = _bypass_proxy_http_clients()

    return FallbackChatModel(
        model=model or s.CHAT_MODEL,
        api_key=api_key or env_key,
        base_url=_normalize_base_url(base_url or env_url) or None,
        temperature=temperature,
        timeout=s.LLM_TIMEOUT,
        max_retries=s.LLM_MAX_RETRIES,
        http_client=sync_client,
        http_async_client=async_client,
    )


@lru_cache(maxsize=4)
def get_vl_llm() -> ChatOpenAI:
    """视觉模型客户端，供图片报告 OCR 兜底使用。

    刻意不走 FallbackChatModel：管理端候选链里配的都是纯文本 chat 模型，
    把图片转发过去只会报"不支持图像输入"，故障转移没有意义。
    凭证与 base_url 复用 chat 的那套解析，换网关只需改 .env。
    """
    s = get_settings()
    api_key, base_url = _resolve_chat_credentials()
    sync_client, async_client = _bypass_proxy_http_clients()
    return ChatOpenAI(
        model=s.VL_MODEL,
        api_key=api_key,
        base_url=base_url or None,
        temperature=0,
        timeout=s.VL_OCR_TIMEOUT,
        max_retries=s.LLM_MAX_RETRIES,
        http_client=sync_client,
        http_async_client=async_client,
    )


@lru_cache(maxsize=1)
def get_async_openai() -> AsyncOpenAI:
    """进程级复用的原生 AsyncOpenAI 客户端，供不走 LangChain 的直连场景使用。"""
    api_key, base_url = _resolve_chat_credentials()
    _, async_client = _bypass_proxy_http_clients()
    return AsyncOpenAI(
        api_key=api_key,
        base_url=base_url or None,
        http_client=async_client,
    )