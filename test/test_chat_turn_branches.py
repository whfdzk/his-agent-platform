# 单轮对话两个分支的确定性单测。
#
# 这两个函数原是 _event_generator 内部嵌套 40 行的闭包，抽成模块级函数后
# 才能脱离"锁 + Redis + SSE 循环"单独验证事件推送逻辑。

import asyncio
import types

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from src.api.routers import chat as chat_router
from src.api.routers.chat import (
    ChatIdentity,
    _resolve_identity,
    _run_inquiry_turn_events,
    _stream_supervisor_turn_events,
)


def _request(host: str = "1.2.3.4"):
    return types.SimpleNamespace(client=types.SimpleNamespace(host=host))


def _identity(user_id: str, patient_id: int | None = None) -> ChatIdentity:
    return ChatIdentity(
        user_id=user_id, authenticated=True, rate_key=f"user:{user_id}",
        patient_id=patient_id, tenant_id=1,
    )


async def _drain(queue: asyncio.Queue) -> list[dict]:
    events = []
    while not queue.empty():
        events.append(await queue.get())
    return events


async def test_inquiry_branch_pushes_clarify_then_reply_then_appointment(monkeypatch):
    async def _fake_inquiry_turn(
        message, thread_id, redis, user_id=None, callbacks=None, persist=True,
        session_id="", tenant_id=None,
    ):
        assert message == "我头疼"
        assert session_id == "s"  # 原始会话 ID 要透传，不能把 thread_id 当前缀带下去
        return "是否伴随发热？", {"appointment_id": 7}, True

    monkeypatch.setattr(chat_router, "_run_inquiry_turn", _fake_inquiry_turn)

    queue: asyncio.Queue = asyncio.Queue()
    await _run_inquiry_turn_events(
        "我头疼", "u:s", object(), "u", None, True, queue, session_id="s",
    )

    assert await _drain(queue) == [
        {"type": "clarify", "question": "是否伴随发热？"},
        {"type": "token", "content": "是否伴随发热？"},
        {"type": "appointment", "data": {"appointment_id": 7}},
    ]


async def test_inquiry_branch_omits_clarify_when_not_asking(monkeypatch):
    async def _fake_inquiry_turn(message, thread_id, redis, **kwargs):
        return "建议挂呼吸内科", None, False

    monkeypatch.setattr(chat_router, "_run_inquiry_turn", _fake_inquiry_turn)

    queue: asyncio.Queue = asyncio.Queue()
    await _run_inquiry_turn_events("我头疼", "u:s", object(), "u", None, True, queue)

    assert await _drain(queue) == [{"type": "token", "content": "建议挂呼吸内科"}]


async def test_supervisor_branch_pushes_only_ai_reply_chunks(monkeypatch):
    class _FakeAgent:
        def astream(self, *args, **kwargs):
            async def _gen():
                yield (AIMessage(content="您"), None)
                yield (AIMessage(content="好"), None)
                # 工具结果与用户回显都不能推给客户端，否则会看到内部中间产物
                yield (ToolMessage(content="没有找到相关记忆", tool_call_id="1"), None)
                yield (HumanMessage(content="我头疼"), None)
                yield (AIMessage(content=""), None)  # 空内容不下发
            return _gen()

    async def _get_agent():
        return _FakeAgent()

    monkeypatch.setattr(chat_router, "get_supervisor_agent", _get_agent)

    queue: asyncio.Queue = asyncio.Queue()
    await _stream_supervisor_turn_events(
        "我头疼", "u:s", None, _identity("1"), "1", "s", queue,
    )

    # 相邻 token 会被缓冲合并后再下发，减少 SSE 帧数量
    assert await _drain(queue) == [{"type": "token", "content": "您好"}]


async def test_supervisor_branch_passes_identity_into_context(monkeypatch):
    seen: list[dict] = []

    class _FakeAgent:
        def astream(self, state, *, config=None, stream_mode=None, context=None):
            seen.append({"user_id": context.user_id, "patient_id": context.patient_id,
                         "authenticated": context.authenticated, "session_id": context.session_id})

            async def _gen():
                yield (AIMessage(content="ok"), None)
            return _gen()

    async def _get_agent():
        return _FakeAgent()

    monkeypatch.setattr(chat_router, "get_supervisor_agent", _get_agent)

    queue: asyncio.Queue = asyncio.Queue()
    await _stream_supervisor_turn_events(
        "我头疼", "u:s", None, _identity("42", patient_id=42), "42", "sess", queue,
    )
    await _drain(queue)

    assert seen == [{
        "user_id": "42", "patient_id": 42, "authenticated": True, "session_id": "sess",
    }]
