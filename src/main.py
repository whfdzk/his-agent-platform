import asyncio
import time

import uvicorn
from fastapi import Depends, FastAPI
from starlette.middleware.cors import CORSMiddleware
from sqlalchemy import text

from src.infra.rustfs_client import aensure_bucket_exists, get_rustfs_client
from src.infra.milvus_client import (
    check_milvus_health,
    close_milvus_client,
    get_milvus_dependency,
)
from src.infra.neo4j_client import (
    check_neo4j_health,
    close_neo4j_driver,
    get_neo4j_driver,
)
from src.core.agent_registry import (
    all_agent_configs,
    refresh_agents as refresh_agent_configs,
    set_provider as set_agent_config_provider,
    start_watcher as start_agent_config_watcher,
    stop_watcher as stop_agent_config_watcher,
)
from src.core.config import get_settings
from src.core.concurrency import to_thread
from src.core.model_registry import refresh_candidates, set_provider
from src.core.persist_queue import set_writer, start_consumer, stop_consumer
from src.modules.ops.model_provider import load_chat_candidates
from src.modules.platform.agent_provider import load_agent_configs, seed_builtin_agents
from src.modules.persist_writer import write_batch
from src.middlewares.logging import LoggingMiddleware
from src.core.exceptions import register_exception_handlers
from src.core.logger import setup_logger
from src.infra.database import engine
from src.infra.embedding import get_embeddings
from src.infra.redis_cache import get_redis_client, close_redis_pools
from loguru import logger
from src.api.routers.api import router as api_router
from src.api.routers.chat import router as chat_router
from src.api.routers.knowledge import router as knowledge_router
from src.api.routers.appointment import router as appointment_router
from src.api.routers.session import router as session_router
from src.api.routers.stats import router as stats_router
from src.api.routers.model_config import router as model_config_router
from src.api.routers.auth import router as auth_router
from src.api.routers.admin_auth import router as admin_auth_router
from src.api.routers.metrics import router as metrics_router
from src.api.routers.patient import router as patient_router
from src.api.routers.reports import router as reports_router
from src.api.routers.health import router as health_router
from src.api.routers.guide import router as guide_router
from src.api.routers.observability import router as observability_router
from src.api.routers.safety import router as safety_router
from src.api.routers.admin_users import router as admin_users_router
from src.api.routers.admin_tenants import router as admin_tenants_router
from src.api.routers.admin_agents import router as admin_agents_router
from src.api.routers.admin_skills import router as admin_skills_router
from src.api.routers.admin_events import router as admin_events_router
from src.api.routers.admin_prompts import router as admin_prompts_router
from src.api.routers.admin_conversations import router as admin_conversations_router
from src.api.routers.admin_system import router as admin_system_router
from src.api.routers.admin_stats import router as admin_stats_router
from src.api.routers.admin_membership import router as admin_membership_router
from src.api.routers.admin_clinician import router as admin_clinician_router
from src.api.routers.voice import router as voice_router
from src.api.routers.clinical_actions import router as clinical_actions_router
from src.api.routers.clinical_events import router as clinical_events_router
from src.api.routers.clinician import router as clinician_router
from src.api.routers.handoff import admin_router as admin_handoff_router, router as handoff_router



# 使用上下文管理器感知项目的生命周期
from contextlib import asynccontextmanager

import re

# PHI 正则：身份证（18 位）/ 手机号（1[3-9] 开头 11 位）
_ID_CARD_RE = re.compile(r"\d{6}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx]")
_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")


def _scrub_sentry_event(event: dict):
    """Sentry 上报前剥离消息内容与 PHI 字段，避免患者隐私出网。"""
    try:
        # 剥离已知可能携带患者消息的字段
        for exc in (event.get("exception") or {}).get("values") or []:
            frames = (exc.get("stacktrace") or {}).get("frames") or []
            for frame in frames:
                if frame.get("vars"):
                    frame["vars"] = {"_redacted": "<stripped>"}
        req = event.get("request") or {}
        if req.get("data"):
            req["data"] = "<stripped>"
        text = json_dumps_safe(event)
        if text:
            text = _ID_CARD_RE.sub("***", _PHONE_RE.sub("***", text))
            event = json_loads_safe(text) or event
    except Exception:
        pass
    return event


def json_dumps_safe(obj) -> str:
    import json
    try:
        return json.dumps(obj, ensure_ascii=False, default=str)
    except Exception:
        return ""


def json_loads_safe(text: str):
    import json
    try:
        return json.loads(text)
    except Exception:
        return None

@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    setup_logger(settings) # 配置日志组件
    logger.info(f"{settings.APP_NAME} 启动.. | 使用环境： {settings.APP_ENV}")
    # Sentry（可选）：配置 SENTRY_DSN 才启用；剥离消息内容防 PHI 上报
    if settings.SENTRY_DSN:
        try:
            import sentry_sdk
            sentry_sdk.init(
                dsn=settings.SENTRY_DSN,
                traces_sample_rate=0.1,
                before_send=lambda event, hint: _scrub_sentry_event(event),
            )
            logger.info("Sentry 已启用")
        except Exception as e:
            logger.warning(f"Sentry 初始化失败: {e}")

    # 应用启动时执行
    # 确保 RustFS 桶的存在
    try:
        await aensure_bucket_exists()
    except Exception as e:
        logger.error(f"rustfs 桶创建失败，文件上传功能将无法使用：{e}")
    try:
        await  asyncio.wait_for(
        to_thread(check_milvus_health),
        timeout=10.0,    # 10 秒连不上就放弃
    )
    except asyncio.TimeoutError:
        logger.error("milvus 连接超时（10s），向量检索功能将无法使用")
    except Exception as e:
        logger.error(f"milvus 连接失败，向量检索功能将无法使用：{e}")
    try:
        await check_neo4j_health()
    except Exception as e:
        logger.error(f"neo4j 连接失败，图谱功能将无法使用：{e}")

    # 预热 embedding：外部 embedding 服务首次 HTTPS 建连实测约 40s（后续 0.08s）。
    # 放到后台 task 里跑，不阻塞启动；否则第一个走向量检索的请求（知识库检索、
    # 体征解读、问诊长期记忆）会干等 40 秒。
    async def _warmup_embedding() -> None:
        try:
            started = time.time()
            emb = get_embeddings()
            await emb.aembed_query("预热")
            logger.info(f"Embedding 预热完成，耗时 {time.time() - started:.1f}s")
        except Exception as e:
            logger.warning(f"Embedding 预热失败（首次调用将较慢）: {type(e).__name__}: {e}")

    app.state.embedding_warmup_task = asyncio.create_task(_warmup_embedding())

    # 预热 BM25 关键词索引：8755 个 chunk 建索引约 9 秒，放在第一个知识查询里
    # 就是实打实的首查延迟。后台建好，请求来的时候直接用。
    async def _warmup_bm25() -> None:
        from src.knowledge.bm25_store import warmup as bm25_warmup

        await bm25_warmup()

    app.state.bm25_warmup_task = asyncio.create_task(_warmup_bm25())

    # 启动批量持久化队列（消息/token_usage 落库）
    # 落库实现由 modules 层注入：core 只排队攒批，不反向依赖上层
    set_writer(write_batch)
    start_consumer()

    # 候选模型链：读库实现同样由 modules 层注入，core 只持有缓存
    set_provider(load_chat_candidates)
    try:
        chain = await refresh_candidates()
        if chain:
            logger.info(
                "模型故障转移链：{} → .env 主模型兜底",
                " → ".join(f"{c.name}({c.model_name})" for c in chain),
            )
    except Exception as e:
        logger.warning(f"候选模型链加载失败，仅使用 .env 主模型: {e}")

    # Agent 配置：读库实现同样由 modules 层注入，core 只持有缓存。
    # 启动时先把内置 Agent 补齐入库（幂等、只补缺失），再加载配置 —— 这样管理端
    # 一打开就能看到全部 Agent，运行时也立刻从库里取配置，而不是用代码里的常量。
    set_agent_config_provider(load_agent_configs)
    try:
        await seed_builtin_agents()
        loaded = await refresh_agent_configs(reason="startup")
        if not loaded.ok:
            logger.warning(f"Agent 配置加载失败，回退代码内置默认: {loaded.error}")
        else:
            logger.info(
                "Agent 配置已加载 v{}（{} 个）: {}",
                loaded.version,
                loaded.count,
                sorted(all_agent_configs().keys()),
            )
        if loaded.rejected:
            logger.warning(
                f"有 {len(loaded.rejected)} 条 Agent 配置未通过装配校验，运行时回退内置默认"
            )
    except Exception as e:
        logger.warning(f"Agent 配置加载失败，回退代码内置默认: {e}")

    # TTL 轮询：本进程的改动靠管理端主动刷新，其他副本改了库只能靠这里跟上
    await start_agent_config_watcher()

    # 事件总线消费组：与业务进程同生命周期。多实例部署时每个实例各起一个
    # consumer（同一 group），Redis 会把消息分给其中一个，天然做了负载均衡。
    from src.modules.events import consumer as event_consumer

    await event_consumer.start()

    yield
    # 应用关闭时执行
    await event_consumer.stop()
    await stop_agent_config_watcher()
    # 关闭数据库连接池
    await stop_consumer()
    await close_neo4j_driver()
    close_milvus_client()
    await engine.dispose()
    await close_redis_pools()
    logger.info(f"{settings.APP_NAME} 关闭.. ")


def create_app() -> FastAPI:
    settings = get_settings()

    # 创建应用
    # 注册生命周期管理器
    app = FastAPI(title=settings.APP_NAME,
                  version="1.0.0",
                  debug=settings.APP_DEBUG,
                  lifespan=lifespan,
                  )


    # 注册中间件
    app.add_middleware(LoggingMiddleware)
    # 注册跨域中间件（来源走环境变量 CORS_ORIGINS，生产不可用默认值兜底）
    app.add_middleware(CORSMiddleware,
                       allow_origins=settings.cors_origin_list,
                       allow_credentials=True,
                       allow_methods=["*"],
                       allow_headers=["*"])



    # 注册异常处理器
    register_exception_handlers(app)

    # 注册路由
    # 注意：chat/knowledge 等路由的 prefix 已自带 /api/v1，
    # 这里不再追加 prefix，避免出现 /api/v1/api/v1/... 双重前缀
    app.include_router(api_router, prefix="/api/v1")  # api_router prefix=/medical，无 /api/v1
    app.include_router(chat_router)
    app.include_router(knowledge_router)
    app.include_router(appointment_router)
    app.include_router(session_router)
    app.include_router(stats_router)
    app.include_router(model_config_router)
    app.include_router(auth_router)
    app.include_router(admin_auth_router)
    app.include_router(metrics_router)
    # 患者端：首页/档案/指标/提醒/隐私 + 报告中心
    app.include_router(patient_router)
    app.include_router(reports_router)
    # 慢病管理（用药记录/健康计划/周期报告）与就医辅助（科室/医院）
    app.include_router(health_router)
    app.include_router(guide_router)
    # 管理端：全链路追踪与 Safety Engine 审计（均带 admin 守卫）
    app.include_router(observability_router)
    app.include_router(safety_router)
    # 管理端平台域：用户 / Agent / Prompt / 对话 / 系统配置 / 统计总览 / 会员（均带 admin 守卫）
    app.include_router(admin_users_router)
    app.include_router(admin_tenants_router)
    app.include_router(admin_agents_router)
    app.include_router(admin_skills_router)
    # 事件总线观测（Redis Stream 流长 / Pending / 死信 + 已处理明细）
    app.include_router(admin_events_router)
    app.include_router(admin_prompts_router)
    app.include_router(admin_conversations_router)
    app.include_router(admin_system_router)
    app.include_router(admin_stats_router)
    app.include_router(admin_membership_router)
    # C4：管理端医护授权（角色 + 科室 + 责任患者）
    app.include_router(admin_clinician_router)
    app.include_router(voice_router)
    # B2：高危动作确认（clinical_actions 状态机）
    app.include_router(clinical_actions_router)
    # B3：急症分级事件（clinical_events）
    app.include_router(clinical_events_router)
    # C4：医护端（clinician 授权范围下的只读视图 + 代批）
    app.include_router(clinician_router)
    # C3：人工接管（工单申请 / 坐席认领回复 / 管理端只读）
    app.include_router(handoff_router)
    app.include_router(admin_handoff_router)


    return app

# 创建fastapi 应用
app = create_app()

if __name__ == "__main__":
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=8000,
        limit_concurrency=100,      # 最大并发请求数，超过则排队
        backlog=200,                # 排队队列长度
        timeout_keep_alive=5,       # Keep-Alive 超时
    )
# 健康检查路由； 能访问通，就代表应用启动
@app.get("/health")
async def root():
    # prometheus 规范约束，返回的 status 必须是 ok，代表健康
    return {"status": "ok"}


@app.get("/health/pool")
async def health_pool():
    """查看数据库连接池状态"""
    pool = engine.pool
    return {
        "database_pool": {
            "pool_size": pool.size(),
            "checked_in": pool.checkedin(),
            "checked_out": pool.checkedout(),
            "overflow": pool.overflow(),
            "total": pool.size() + pool.overflow(),
        }
    }


@app.get("/health/deps")
async def health_deps():
    """检查核心依赖可用性（PostgreSQL / Redis / RustFS / Milvus / Neo4j）。并发检查，P95 < 200ms。

    注意：不要在 Depends 层注入 Milvus 连接——connections.connect 是同步阻塞调用，
    依赖注入发生在进入端点之前，Milvus 不可用时会直接把整个健康检查拖死（500 或长挂起）。
    这里改为在端点内部用线程池 + 超时探测，保证任何一个依赖异常都不影响整体返回。
    """
    settings = get_settings()

    async def check_postgres():
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return {"ok": True, "error": ""}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    async def check_redis():
        try:
            redis_client = await get_redis_client()
            await redis_client.ping()
            return {"ok": True, "error": ""}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    async def check_rustfs():
        try:
            rustfs_client = get_rustfs_client()
            await to_thread(rustfs_client.head_bucket, Bucket=settings.RUSTFS_BUCKET)
            return {"ok": True, "error": ""}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    async def check_milvus():
        try:
            await asyncio.wait_for(to_thread(check_milvus_health), timeout=5.0)
            return {"ok": True, "error": ""}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    async def check_neo4j():
        try:
            neo4j_driver = get_neo4j_driver()
            await asyncio.wait_for(neo4j_driver.verify_connectivity(), timeout=5.0)
            return {"ok": True, "error": ""}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    # 并发检查 5 个依赖
    results = await asyncio.gather(
        check_postgres(), check_redis(), check_rustfs(), check_milvus(), check_neo4j()
    )

    result = {
        "status": "ok",
        "dependencies": {
            "postgres": results[0],
            "redis": results[1],
            "rustfs": results[2],
            "milvus": results[3],
            "neo4j": results[4],
        },
    }

    if any(not item["ok"] for item in result["dependencies"].values()):
        result["status"] = "degraded"

    return result