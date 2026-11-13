import pytest
from langchain_core.messages import AIMessage, HumanMessage

from src.agents.inquiry import graph as inquiry_graph
from src.agents.inquiry.state import InquiryState


class _FakeGraph:
    """只回显传入 state 的假图，用于覆盖 run_inquiry 的消息装配逻辑。"""

    def __init__(self):
        self.captured = None

    async def ainvoke(self, state, config=None):
        self.captured = state
        state.messages.append(AIMessage(content="请问疼痛位置？"))
        return state


@pytest.mark.asyncio
async def test_run_inquiry_appends_human_message(monkeypatch):
    fake = _FakeGraph()
    monkeypatch.setattr(inquiry_graph, "build_inquiry_graph", lambda deps: fake)

    reply, new_state = await inquiry_graph.run_inquiry(
        user_message="我肚子疼",
        thread_id="1:session",
        deps=None,
    )

    assert isinstance(new_state.messages[0], HumanMessage)
    assert new_state.messages[0].content == "我肚子疼"
    assert reply == "请问疼痛位置？"
    # session_id 不能带上 thread_id 的 user 前缀，否则落库后前端回放会再拼一层
    assert new_state.session_id == "session"


@pytest.mark.asyncio
async def test_run_inquiry_uses_raw_session_id(monkeypatch):
    fake = _FakeGraph()
    monkeypatch.setattr(inquiry_graph, "build_inquiry_graph", lambda deps: fake)

    _reply, new_state = await inquiry_graph.run_inquiry(
        user_message="我头疼",
        thread_id="1:abc123",
        deps=None,
        session_id="abc123",
    )

    assert new_state.session_id == "abc123"


@pytest.mark.asyncio
async def test_run_inquiry_reuses_existing_state(monkeypatch):
    fake = _FakeGraph()
    monkeypatch.setattr(inquiry_graph, "build_inquiry_graph", lambda deps: fake)

    existing = InquiryState(session_id="1:session", round=1)
    _reply, new_state = await inquiry_graph.run_inquiry(
        user_message="右下腹",
        thread_id="1:session",
        deps=None,
        existing_state=existing,
    )

    assert new_state is existing
    assert isinstance(new_state.messages[0], HumanMessage)
    assert new_state.messages[0].content == "右下腹"
