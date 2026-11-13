import pytest

from src.agents.workers.inquiry_agent import get_inquiry_agent

# 依赖真实 LLM，pytest.ini 默认跳过，需要时用 `pytest -m integration` 执行
pytestmark = pytest.mark.integration


async def test_inquiry():
    agent = get_inquiry_agent()
    result = await agent.ainvoke({
        "messages": [{"role": "user", "content": "我头疼发烧两天了，体温38.5度"}]
    })
    print(result["messages"][-1].content)