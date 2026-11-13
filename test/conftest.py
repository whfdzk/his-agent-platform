# 确定性单测共用的测试替身。
# 这里只实现聊天链路真正用到的 Redis 命令，避免单测依赖真实 Redis。

from __future__ import annotations

import pytest

from src.utils import token_revocation


class FakeRedis:
    """内存版 Redis 替身（get/set/delete/exists/incr）。

    与 redis-py 的差异点已在实现中对齐：
    - `set(..., nx=True)` 在 key 已存在时返回 None，成功返回 True
    - `get` 未命中返回 None（非 bytes，业务代码一律按 str 处理）
    """

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.counters: dict[str, int] = {}

    async def get(self, key: str):
        return self.store.get(key)

    async def set(self, key: str, value, nx: bool = False, ex: int | None = None, px: int | None = None):
        if nx and key in self.store:
            return None
        self.store[key] = str(value)
        return True

    async def delete(self, *keys: str) -> int:
        removed = 0
        for key in keys:
            if key in self.store:
                del self.store[key]
                removed += 1
        return removed

    async def exists(self, key: str) -> int:
        return 1 if key in self.store else 0

    async def incr(self, key: str) -> int:
        self.counters[key] = self.counters.get(key, 0) + 1
        return self.counters[key]


class BytesRedis(FakeRedis):
    """模拟 checkpointer 连接池（decode_responses=False）的返回类型。

    聊天路由复用的正是 bytes 模式的客户端，用它覆盖"bytes 与 str 比较
    恒不相等"这类只在生产环境才暴露的缺陷。
    """

    async def get(self, key: str):
        value = await super().get(key)
        return value.encode("utf-8") if value is not None else None


class CountingRedis(FakeRedis):
    """统计读写次数的替身，用于证明"逐 token 分配 seq"这类隐性放大已被消除。"""

    def __init__(self) -> None:
        super().__init__()
        self.reads = 0
        self.writes = 0

    async def incr(self, key: str) -> int:
        self.reads += 1
        return await super().incr(key)

    async def get(self, key: str):
        self.reads += 1
        return await super().get(key)

    async def set(self, key: str, value, nx: bool = False, ex: int | None = None, px: int | None = None):
        self.writes += 1
        return await super().set(key, value, nx=nx, ex=ex, px=px)


@pytest.fixture
def redis() -> FakeRedis:
    return FakeRedis()


@pytest.fixture
def counting_redis() -> CountingRedis:
    return CountingRedis()


@pytest.fixture
def bytes_redis() -> BytesRedis:
    return BytesRedis()


# ── 数据库替身 ────────────────────────────────────────────────────────────
# 患者端工具（用药/健康/就医）都在函数内开短会话 `async with AsyncSessionLocal()`，
# 单测不能连真实 PG：这里给一个只实现被用到那几个方法的替身。

class FakeDBResult:
    """execute() 的返回值替身：按测试预置的方式兑现结果。"""

    def __init__(self, scalar=None, rows=None, first=None) -> None:
        self._scalar = scalar
        self._rows = rows or []
        self._first = first

    def scalar_one_or_none(self):
        return self._scalar

    def scalar_one(self):
        return self._scalar

    def scalar(self):
        return self._scalar

    def scalars(self):
        return self

    def all(self):
        return self._rows

    def first(self):
        return self._first


class FakeDBSession:
    """一次请求里可能连开多个查询：results 按顺序依次兑现，用尽后回落到 result。"""

    def __init__(
        self,
        result: FakeDBResult | None = None,
        entity=None,
        results: list[FakeDBResult] | None = None,
    ) -> None:
        self.result = result or FakeDBResult()
        self.results = list(results or [])
        self.entity = entity
        self.added: list = []
        self.committed = 0
        self.deleted: list = []

    async def execute(self, _stmt):
        return self.results.pop(0) if self.results else self.result

    async def get(self, _model, _pk):
        return self.entity

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.committed += 1

    async def refresh(self, _obj):
        return None

    async def delete(self, obj):
        self.deleted.append(obj)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


@pytest.fixture
def fake_db(monkeypatch):
    """把 AsyncSessionLocal 换成内存替身；返回工厂，便于一个用例里换不同结果。"""
    from src.infra import database

    def install(
        result: FakeDBResult | None = None,
        entity=None,
        results: list[FakeDBResult] | None = None,
    ) -> FakeDBSession:
        """result 给 execute() 用（results 按顺序兑现多个查询），entity 给 get() 用。"""
        db = FakeDBSession(result=result, entity=entity, results=results)
        monkeypatch.setattr(database, "AsyncSessionLocal", lambda: db)
        return db

    return install


class FakeRuntime:
    """ToolRuntime 替身：只需要 context 与 config 两个属性。"""

    def __init__(self, user_id: str = "2", session_id: str = "s1") -> None:
        from src.core.context import UserContext

        self.context = UserContext(user_id=user_id, session_id=session_id)
        self.config = {"configurable": {"thread_id": f"{user_id}:{session_id}"}}


@pytest.fixture
def runtime_factory():
    return FakeRuntime


def coroutine_of(tool_obj):
    """取 @tool 包装下的原始协程函数（异步工具的入口）。"""
    return getattr(tool_obj, "coroutine", None) or tool_obj.func


# ── 鉴权撤销替身 ──────────────────────────────────────────────────────────
# 鉴权链路每个请求都会查一次 Redis 撤销名单（登出黑名单）。
# 默认单测不连外部服务：统一视为"未撤销 / 写入成功"，保持确定性；
# 需要验证撤销语义的用例可在测试内部再次 monkeypatch 覆盖这层 stub。

@pytest.fixture(autouse=True)
def _stub_token_revocation(monkeypatch):
    async def _not_revoked(*_args, **_kwargs) -> bool:
        return False

    async def _ok(*_args, **_kwargs) -> bool:
        return True

    monkeypatch.setattr(token_revocation, "is_jti_revoked", _not_revoked)
    monkeypatch.setattr(token_revocation, "is_session_revoked", _not_revoked)
    monkeypatch.setattr(token_revocation, "revoke_jti", _ok)
    monkeypatch.setattr(token_revocation, "revoke_session_before", _ok)
