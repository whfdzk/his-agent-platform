# src/core/agent_registry.py
#
# Agent 运行时配置注册表：把「Agent 有哪些可配置项」变成一份显式的数据结构。
#
# 分层约束：core 是最底层，不能 import modules / infra（见 test_layering.py），
# 而 Agent 配置存在 agents 表里。所以这里只定义数据结构与缓存，
# 真正的读库实现由 modules 层通过 set_provider 注入（与 model_registry 同一套路）。
#
# 配置来源语义（与前端「草稿 / 已发布 / 已停用」状态对应）：
#   published → 使用库里的配置装配 Agent
#   draft / disabled / 库里没有该 code → 回退代码内置默认（保证升级后行为不变）
# 这样「发布」才真正等于「配置生效」，也能做到保存即热更新（下一次取 Agent 时重建）。
#
# 热加载机制（两条通道，互补）：
#   1. 主动推送：管理端任何写操作提交后调 refresh_agents()，本进程下一个请求即生效；
#   2. TTL 轮询：后台 watcher 每 AGENT_CONFIG_REFRESH_SECONDS 秒刷一次，
#      兜住「另一个副本 / 另一台机器改了库」的情况，本进程最多滞后一个周期。
#
# 生效时机（重要）：缓存替换后，**下一次** get_*_agent() 取配置时才重建 Agent；
# 已经在飞行的 ainvoke 继续用旧实例跑完 —— 单轮对话内的 system_prompt 不会中途变化。

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone

from loguru import logger

# Agent 发布状态（与 modules/platform/model.py 的常量保持一致，core 不反向依赖）
AGENT_STATUS_PUBLISHED = "published"

# 配置缓存多久（秒）。管理端改完配置主动刷新；这里只兜住"另一个副本改了库"的情况。
DEFAULT_REFRESH_INTERVAL = 60.0


@dataclass(frozen=True)
class SummarizationConfig:
    """会话总结压缩参数（对应 langchain SummarizationMiddleware 的三个魔法值）。"""

    enabled: bool = True
    trigger_tokens: int = 4000
    trigger_messages: int = 6
    keep_messages: int = 6

    @classmethod
    def from_dict(cls, value: object) -> "SummarizationConfig":
        """容错解析：前端/历史数据里字段缺失或类型不对时退回默认值，不抛异常。"""
        if not isinstance(value, dict):
            return cls()
        base = cls()
        return cls(
            enabled=bool(value.get("enabled", base.enabled)),
            trigger_tokens=_positive_int(value.get("trigger_tokens"), base.trigger_tokens),
            trigger_messages=_positive_int(value.get("trigger_messages"), base.trigger_messages),
            keep_messages=_positive_int(value.get("keep_messages"), base.keep_messages),
        )

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "trigger_tokens": self.trigger_tokens,
            "trigger_messages": self.trigger_messages,
            "keep_messages": self.keep_messages,
        }


@dataclass(frozen=True)
class AgentRuntimeConfig:
    """一个 Agent 的完整运行时配置（不可变，可作为缓存指纹直接比较）。

    字段来源：
    - code / name / agent_type / status / description → agents 表列
    - system_prompt → prompts.active_version 的正文，或 config.system_prompt 内联文本
    - model / base_url / api_key / temperature → config.model_config_id 关联的 model_configs，
      或 config.model 直接指定的模型名，最终回退 .env 默认模型
    - tool_ids → 可用工具白名单（空 = 用代码内置默认）
    - sub_agent_codes → supervisor 可调度的 worker（空 = 内置默认）
    """

    code: str
    name: str = ""
    agent_type: str = "worker"
    status: str = "draft"
    description: str = ""

    # 提示词
    system_prompt: str = ""
    prompt_id: int | None = None
    # 激活版本号（prompt_versions.version，形如 "v3"）；内联提示词或回退内置时为 ""
    prompt_version: str = ""

    # 模型
    model_config_id: int | None = None
    model: str = ""
    base_url: str = ""
    api_key: str = field(default="", repr=False)
    temperature: float | None = None

    # 运行参数
    max_turns: int = 0
    tool_ids: tuple[str, ...] = ()
    skill_ids: tuple[str, ...] = ()
    sub_agent_codes: tuple[str, ...] = ()
    degrade_message: str = ""
    summarization: SummarizationConfig = field(default_factory=SummarizationConfig)

    # 配置版本指纹（updated_at），仅用于日志排查
    revision: str = ""

    @property
    def is_active(self) -> bool:
        """只有已发布的 Agent 才用库里的配置；草稿/停用回退代码内置默认。"""
        return self.status == AGENT_STATUS_PUBLISHED

    @property
    def agent_name(self) -> str:
        """create_agent(name=...) 使用的名字，保持历史命名（如 drug_agent）。"""
        return self.code if self.code.endswith("_agent") else f"{self.code}_agent"

    def fallback(self) -> "AgentRuntimeConfig":
        """去掉所有库配置项的副本：装配时当作"没有库配置"使用。"""
        return AgentRuntimeConfig(code=self.code, agent_type=self.agent_type)


@dataclass(frozen=True)
class ConfigIssue:
    """一条配置级问题（装配期校验的结果）。

    fatal=True：该 Agent 的库配置整体不生效，运行时回退代码内置默认。
    这样做而不是"忽略有问题的部分"，是为了避免"看起来生效了、其实少了一半工具"
    的中间态 —— 能力边界的变化必须让管理端看得见。
    """

    code: str
    field: str
    message: str
    fatal: bool = False


@dataclass(frozen=True)
class AgentLoadResult:
    """provider 的返回结构：配置 + 装配期发现的问题。

    允许 provider 直接返回 list[AgentRuntimeConfig]（兼容旧实现与单测），
    此时视为没有问题。
    """

    configs: tuple[AgentRuntimeConfig, ...]
    issues: tuple[ConfigIssue, ...] = ()


@dataclass(frozen=True)
class RefreshResult:
    """一次刷新的结果，供管理端回执与日志使用。"""

    ok: bool
    version: int = 0
    changed: bool = False
    count: int = 0
    rejected: tuple[ConfigIssue, ...] = ()
    error: str = ""
    skipped: str = ""
    duration_ms: int = 0

    def brief(self) -> dict:
        return {
            "ok": self.ok,
            "version": self.version,
            "changed": self.changed,
            "count": self.count,
            "skipped": self.skipped,
            "error": self.error,
            "rejected": [
                {"code": i.code, "field": i.field, "message": i.message}
                for i in self.rejected
            ],
        }

    def describe(self, ok_text: str) -> str:
        """把刷新结果翻译成管理端可直接展示的一句话。

        不能一律说"已生效"：刷新失败时服务仍在用上一版配置，管理端必须知道。
        """
        if not self.ok:
            return (
                f"{ok_text}；但运行时重载失败（{self.error or '未知原因'}），"
                f"服务继续使用上一版配置，系统会自动重试"
            )
        if self.rejected:
            codes = "、".join(sorted({i.code for i in self.rejected}))
            return f"{ok_text}；配置已写入，但 {codes} 未通过装配校验，运行时回退内置默认"
        if not self.changed:
            return f"{ok_text}；配置内容无变化"
        return f"{ok_text}（已生效 v{self.version}）"


def _positive_int(value: object, default: int) -> int:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _str_tuple(value: object) -> tuple[str, ...]:
    """容错解析字符串数组：非字符串元素（null / 数字 / 嵌套对象）一律忽略。"""
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(item.strip() for item in value if isinstance(item, str) and item.strip())


def _optional_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def parse_agent_config(
    code: str,
    config: object,
    *,
    name: str = "",
    agent_type: str = "worker",
    status: str = "draft",
    description: str = "",
    prompt_content: str = "",
    prompt_version: str = "",
    model_name: str = "",
    model_base_url: str = "",
    model_api_key: str = "",
    revision: str = "",
) -> AgentRuntimeConfig:
    """把 agents.config(JSONB) + 关联表数据解析成运行时配置。

    解析一律容错：任何字段类型不对都退回默认值，绝不让一条脏配置把整个 Agent 打挂。
    """
    data = config if isinstance(config, dict) else {}

    # 提示词优先级：prompts 里已激活的版本 > config.system_prompt 内联文本。
    # 这样在「提示词管理」里改版本、激活后，所有关联该 Prompt 的 Agent 立即跟着变。
    system_prompt = (prompt_content or "").strip() or str(data.get("system_prompt") or "").strip()

    model = str(data.get("model") or "").strip() or model_name
    prompt_id = data.get("prompt_id")
    model_config_id = data.get("model_config_id")

    return AgentRuntimeConfig(
        code=code,
        name=name,
        agent_type=agent_type,
        status=status,
        description=description,
        system_prompt=system_prompt,
        prompt_id=prompt_id if isinstance(prompt_id, int) else None,
        prompt_version=(prompt_version or "").strip(),
        model_config_id=model_config_id if isinstance(model_config_id, int) else None,
        model=model,
        base_url=(model_base_url or "").strip(),
        api_key=(model_api_key or "").strip(),
        temperature=_optional_float(data.get("temperature")),
        max_turns=_positive_int(data.get("max_turns"), 0),
        tool_ids=_str_tuple(data.get("tool_ids")),
        skill_ids=_str_tuple(data.get("skill_ids")),
        sub_agent_codes=_str_tuple(data.get("sub_agent_codes")),
        degrade_message=str(data.get("degrade_message") or "").strip(),
        summarization=SummarizationConfig.from_dict(data.get("summarization")),
        revision=revision,
    )


Provider = Callable[[], Awaitable[object]]

# 当前生效配置。语义：只整体替换，绝不原地修改 ——
# 读者拿到的引用要么是旧快照、要么是新快照，不会看到构建到一半的状态。
_cache: dict[str, AgentRuntimeConfig] = {}
_provider: Provider | None = None

_loaded_at = 0.0            # monotonic，用于 TTL 判断
_loaded_wall = ""           # 人类可读的生效时间（UTC ISO）
_version = 0                # 配置版本号：指纹变化才自增
_fingerprint_value = ""     # 当前生效配置的内容指纹
_last_error = ""            # 最近一次刷新失败原因（成功后清空）
_invalid: tuple[ConfigIssue, ...] = ()   # 最近一次刷新中被拒绝的配置
_last_reason = ""           # 最近一次刷新来源：startup / admin / poll / manual

# refresh 是异步临界区（中间有 await），并发进来会交错覆盖缓存，必须串行化。
# Lock 绑定 event loop：pytest 每个用例一个 loop，所以要按 loop 重建。
_lock: asyncio.Lock | None = None
_lock_loop: asyncio.AbstractEventLoop | None = None
_watcher: asyncio.Task | None = None


def set_provider(provider: Provider) -> None:
    """注入读库实现（modules 层负责，见 src/modules/platform/agent_provider.py）。"""
    global _provider
    _provider = provider


def version() -> int:
    return _version


def cache_age() -> float:
    return 0.0 if _loaded_at == 0.0 else time.monotonic() - _loaded_at


def default_refresh_interval() -> float:
    """轮询周期（秒）：读配置，读不到就用内置默认。0 表示关闭轮询。"""
    try:
        from src.core.config import get_settings

        return float(get_settings().AGENT_CONFIG_REFRESH_SECONDS)
    except Exception:
        return DEFAULT_REFRESH_INTERVAL


def all_agent_configs() -> dict[str, AgentRuntimeConfig]:
    """当前缓存的全部配置（只读快照，供管理端展示与排查）。"""
    return dict(_cache)


def get_agent_config(code: str) -> AgentRuntimeConfig:
    """按 code 取库里的原始配置（不区分状态），未命中时返回空壳。

    调用方一般用 get_active_config()：草稿/停用状态不应该影响线上。
    """
    return _cache.get(code) or AgentRuntimeConfig(code=code)


def get_active_config(code: str) -> AgentRuntimeConfig:
    """取真正生效的配置：未发布或未入库时返回"无库配置"的空壳。"""
    cfg = _cache.get(code)
    if cfg is None or not cfg.is_active:
        return AgentRuntimeConfig(code=code)
    return cfg


def runtime_status() -> dict:
    """当前热加载状态（管理端「配置生效情况」面板用）。"""
    interval = default_refresh_interval()
    age = cache_age()
    return {
        "version": _version,
        "fingerprint": _fingerprint_value,
        "loaded_at": _loaded_wall or None,
        "cache_age_seconds": round(age, 1) if _loaded_at else None,
        "refresh_interval_seconds": interval,
        "polling_enabled": interval > 0 and _watcher is not None and not _watcher.done(),
        "next_refresh_in_seconds": (
            round(max(0.0, interval - age), 1) if interval > 0 and _loaded_at else None
        ),
        "last_reason": _last_reason or None,
        "last_error": _last_error or None,
        "rejected": [
            {"code": i.code, "field": i.field, "message": i.message}
            for i in _invalid
        ],
        "agents": [
            {
                "code": cfg.code,
                "status": cfg.status,
                "revision": cfg.revision,
                "config_source": "database" if cfg.is_active else "builtin",
            }
            for cfg in sorted(_cache.values(), key=lambda c: c.code)
        ],
    }


def _current_lock() -> asyncio.Lock:
    global _lock, _lock_loop
    loop = asyncio.get_running_loop()
    if _lock is None or _lock_loop is not loop:
        _lock = asyncio.Lock()
        _lock_loop = loop
    return _lock


def _fingerprint_of(configs: dict[str, AgentRuntimeConfig]) -> str:
    """内容指纹：靠 repr（api_key 声明了 repr=False，不会进指纹也不会进日志）。

    不能只用 revision：切换 prompts.active_version 改的是 prompts 表，
    agents.updated_at 不变，但实际生效的 system_prompt 已经变了。
    用 sha1 而不是内置 hash：内置 hash 受 PYTHONHASHSEED 影响，重启后指纹会变。
    """
    digest = hashlib.sha1()
    for code in sorted(configs):
        digest.update(repr(configs[code]).encode("utf-8"))
        digest.update(b"\x1e")
    return digest.hexdigest()[:16]


def _normalize_load_result(raw: object) -> tuple[list[AgentRuntimeConfig], list[ConfigIssue]]:
    """兼容两种 provider 返回值：AgentLoadResult（带问题）或纯 list（旧实现/单测）。"""
    if isinstance(raw, AgentLoadResult):
        return list(raw.configs), list(raw.issues)
    if isinstance(raw, (list, tuple)):
        return [c for c in raw if isinstance(c, AgentRuntimeConfig)], []
    logger.warning(f"Agent 配置读取返回了不支持的类型：{type(raw).__name__}")
    return [], []


def _log_diff(previous: dict[str, AgentRuntimeConfig], current: dict[str, AgentRuntimeConfig]) -> None:
    added = sorted(set(current) - set(previous))
    removed = sorted(set(previous) - set(current))
    changed = sorted(
        code for code in set(current) & set(previous) if current[code] != previous[code]
    )
    parts = []
    if added:
        parts.append(f"新增 {added}")
    if removed:
        parts.append(f"移除 {removed}")
    if changed:
        parts.append(f"变更 {changed}")
    if parts:
        logger.info(f"Agent 配置已热更新 v{_version}（{_last_reason}）：{'，'.join(parts)}")
    else:
        logger.debug(f"Agent 配置刷新无内容变化（{_last_reason}），保持 v{_version}")


async def refresh_agents(
    *,
    reason: str = "manual",
    max_age: float | None = None,
) -> RefreshResult:
    """刷新配置缓存：读取 → 校验 → 原子替换。

    - reason：来源标记（startup/admin/poll/manual），只进日志，便于排查"谁改的"；
    - max_age：拿到锁后若缓存仍然新鲜则跳过（避免轮询与主动刷新撞车时重复读库）；
    - 读库失败 / 全批失败：保留上一次的好配置继续服务，并记录 last_error。
    """
    global _cache, _loaded_at, _loaded_wall, _version
    global _fingerprint_value, _last_error, _invalid, _last_reason

    if _provider is None:
        return RefreshResult(ok=False, version=_version, skipped="provider_not_set",
                             error="未注入配置读取实现")

    started = time.perf_counter()
    async with _current_lock():
        # 锁内双检：等锁期间别人可能刚刷过，TTL 场景不必再读一次库
        if max_age is not None and _cache and cache_age() < max_age:
            return RefreshResult(ok=True, version=_version, skipped="still_fresh",
                                 count=len(_cache))

        try:
            raw = await _provider()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _last_error = f"{type(e).__name__}: {e}"
            _last_reason = reason
            logger.error(
                f"Agent 配置刷新失败（{reason}），保留 v{_version} 的旧配置继续服务：{_last_error}"
            )
            return RefreshResult(
                ok=False, version=_version, count=len(_cache), error=_last_error,
                duration_ms=int((time.perf_counter() - started) * 1000),
            )

        configs, issues = _normalize_load_result(raw)

        accepted: dict[str, AgentRuntimeConfig] = {}
        rejected: list[ConfigIssue] = []
        for cfg in configs:
            fatal = [i for i in issues if i.code == cfg.code and i.fatal]
            if fatal:
                rejected.extend(fatal)
                for issue in fatal:
                    logger.error(
                        f"Agent 配置被拒绝（{cfg.code}.{issue.field}）：{issue.message}；"
                        f"该 Agent 回退代码内置默认"
                    )
                continue
            accepted[cfg.code] = cfg

        for issue in (i for i in issues if not i.fatal):
            logger.warning(f"Agent 配置降级（{issue.code}.{issue.field}）：{issue.message}")

        previous, previous_fp = _cache, _fingerprint_value
        new_fp = _fingerprint_of(accepted)
        changed = new_fp != previous_fp

        # 原子替换点：一次引用赋值，读者不可能看到半成品
        _cache = accepted
        _loaded_at = time.monotonic()
        _loaded_wall = datetime.now(timezone.utc).isoformat(timespec="seconds")
        _last_error = ""
        _invalid = tuple(rejected)
        _last_reason = reason
        if changed:
            _version += 1
            _fingerprint_value = new_fp
            _log_diff(previous, accepted)

        if not accepted and configs:
            logger.error(
                f"Agent 配置全部被拒绝（{len(configs)} 条），所有 Agent 将回退代码内置默认；"
                f"请检查工具名与子 Agent 编码是否与当前版本匹配"
            )

        return RefreshResult(
            ok=True,
            version=_version,
            changed=changed,
            count=len(accepted),
            rejected=tuple(rejected),
            duration_ms=int((time.perf_counter() - started) * 1000),
        )


async def maybe_refresh(interval: float | None = None) -> bool:
    """TTL 兜底：缓存超过 interval 秒才真正读库。返回是否发生了刷新。"""
    if _provider is None:
        return False
    limit = default_refresh_interval() if interval is None else interval
    if _cache and limit > 0 and cache_age() < limit:
        return False
    result = await refresh_agents(reason="poll", max_age=limit if limit > 0 else None)
    return result.ok and result.skipped != "still_fresh"


async def get_configs(refresh_interval: float = DEFAULT_REFRESH_INTERVAL) -> dict[str, AgentRuntimeConfig]:
    """取配置；超过刷新间隔就顺带刷新一次（异步路径用这个兜住多副本不同步）。"""
    await maybe_refresh(refresh_interval)
    return _cache


async def _watch_loop(interval: float) -> None:
    while True:
        await asyncio.sleep(interval)
        try:
            await maybe_refresh(interval)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # 轮询任务绝不允许因为单次异常而退出
            logger.warning(f"Agent 配置轮询异常（已跳过本轮）：{type(e).__name__}: {e}")


async def start_watcher(interval: float | None = None) -> bool:
    """启动后台轮询任务；interval<=0 时不启动。重复调用是幂等的。"""
    global _watcher
    limit = default_refresh_interval() if interval is None else interval
    if limit <= 0:
        logger.info("Agent 配置轮询未启用（AGENT_CONFIG_REFRESH_SECONDS<=0）")
        return False
    if _watcher is not None and not _watcher.done():
        return False
    _watcher = asyncio.create_task(_watch_loop(limit), name="agent-config-watcher")
    logger.info(f"Agent 配置轮询已启动：每 {limit:g}s 检查一次")
    return True


async def stop_watcher() -> None:
    """停止后台轮询（应用关闭时调用）。"""
    global _watcher
    task, _watcher = _watcher, None
    if task is None:
        return
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


def reset() -> None:
    """仅测试用：清空缓存与注入的实现。"""
    global _provider, _cache, _loaded_at, _loaded_wall, _version
    global _fingerprint_value, _last_error, _invalid, _last_reason, _lock, _lock_loop
    _provider = None
    _cache = {}
    _loaded_at = 0.0
    _loaded_wall = ""
    _version = 0
    _fingerprint_value = ""
    _last_error = ""
    _invalid = ()
    _last_reason = ""
    _lock = None
    _lock_loop = None


def with_overrides(cfg: AgentRuntimeConfig, **kwargs) -> AgentRuntimeConfig:
    """构造派生配置（种子初始化、单测用）。"""
    return replace(cfg, **kwargs)
