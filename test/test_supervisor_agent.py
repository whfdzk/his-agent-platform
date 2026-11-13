import uuid

import pytest

from src.agents.supervisor_agent import chat_endpoint, create_supervisor_agent

# 本文件全部依赖真实 LLM / Redis / Milvus，pytest.ini 默认跳过；
# 需要时用 `pytest -m integration` 单独执行，`-m "not integration"` 才会排除。
pytestmark = pytest.mark.integration


async def test_supervisor_agent():
    result = await chat_endpoint("1234","AEEEEA", "你好，我是张晓晓")
    print(result)


async def test_agent_memory():
    """验证短期记忆：同一 thread_id 下第二轮能记住第一轮的内容"""
    # 第一轮：自我介绍
    reply1 = await chat_endpoint("1123", "ATDAAS", "你好，我叫张晓晓")
    print(f"\n第一轮回复：{reply1}")

    # 第二轮：考察记忆
    reply2 = await chat_endpoint("1123", "ATDAAS", "我是谁？")
    print(f"第二轮回复：{reply2}")

    # 断言：第二轮回复应包含名字
    assert "张晓晓" in reply2, f"Agent 应该记住用户名字，实际回复：{reply2}"

    # 第三轮：新会话
    reply3 = await chat_endpoint("1123", "7783AAA", "我是谁？")
    print(f"第三轮回复：{reply3}")


def new_session() -> str:
    """每次测试生成唯一 session_id，防止不同测试/不同执行轮次的 checkpoint 互相污染。"""
    return uuid.uuid4().hex[:8]


async def test_agent_store():
    """验证长期记忆：跨会话记忆
    第一个 session 写入病史 → 第二个全新 session 能检索到
    """
    # ── 第一轮：用新会话写入病史 ──────────────────────────────────────────
    session_1 = new_session()
    resp1 = await chat_endpoint("2244", session_1, "你好，我叫张三，有新冠病史")
    print(f"\n[写入] 回复：{resp1}")
    resp1 = await chat_endpoint("2244", session_1, "我对樱桃过敏")
    print(f"\n[写入] 回复：{resp1}")

    # ── 第二轮：换一个全新会话，查询病史（跨会话长期记忆） ───────────────
    session_2 = new_session()  # 与 session_1 不同，短期记忆里没有上文
    resp2 = await chat_endpoint("2244", session_2, "我有什么病史和过敏史？")
    print(f"[检索] 回复：{resp2}")

    # 断言内容必须与上面写入的数据一致：本用例种的是新冠病史 + 樱桃过敏
    assert "新冠" in resp2 or "樱桃" in resp2, f"长期记忆应能跨会话检索到病史/过敏史，实际回复：{resp2}"

# **测试 Supervisor 调用 Worker**
async def test_supervisor():
    agent = await create_supervisor_agent()
    config = {"configurable": {"thread_id": "test_001"}}

    # 第一轮：问诊场景 → 应触发 call_inquiry_agent
    r1 = await agent.ainvoke(
        {"messages": [{"role": "user", "content": "我最近头疼发烧，不知道该挂什么科"}]},
        config=config,
    )
    print("=== 第一轮（问诊）===")
    print(r1["messages"][-1].content)

    # 第二轮：药物场景 → 应触发 call_drug_agent
    r2 = await agent.ainvoke(
        {"messages": [{"role": "user", "content": "医生开了布洛芬和阿莫西林，可以一起吃吗？"}]},
        config=config,
    )
    print("=== 第二轮（药物）===")
    print(r2["messages"][-1].content)

    # 第三轮：知识问答 → 应触发 call_knowledge_agent
    r3 = await agent.ainvoke(
        {"messages": [{"role": "user", "content": "感冒一般多久能好？"}]},
        config=config,
    )
    print("=== 第三轮（知识）===")
    print(r3["messages"][-1].content)
