## Redis 中存储的数据解析
### LangGraph 在 Redis 中创建的 Key 种类
`AsyncRedisSaver` 使用 **RedisJSON** 存储数据，并用 **RediSearch** 建立索引。首次调用 `checkpointer.asetup()` 会自动创建两个索引：

| 索引名           | 对应的 Key 前缀      | 作用                                               |
| ---------------- | -------------------- | -------------------------------------------------- |
| checkpoint       | checkpoint$...       | 存储每个 thread 的检查点快照（消息列表等完整状态） |
| checkpoint_write | checkpoint_write$... | 存储检查点的"写操作日志"（中间步骤的增量记录）     |

## **Key 命名格式详解**

打开 RedisInsight，你会看到类似这样的 key：

```plain text
checkpoint$1123:ATDAAS$1efb84c2-c7a2-6fb0-bffc-087a50b63ff8$
checkpoint_write$1123:ATDAAS$1efb84c2-c7a2-6fb0-bffc-087a50b63ff8$0$agent
```

格式拆解：**parent_checkpoint_id：通过这个引用，最终可以生成一个有序的对话列表**

```plain text
{索引前缀}${thread_id}${checkpoint_id}${...}

checkpoint$          → 这是一个检查点快照
  1123:ATDAAS        → thread_id（你代码里的 f"{user_id}:{session_id}"）
  1efb84c2-...       → checkpoint_id（LangGraph 自动生成的 UUID，每轮对话生成一个）
  （结尾空）         → parent_checkpoint_id（第一轮为空，后续轮次指向上一个快照）

checkpoint_write$    → 这是一个写操作日志
  1123:ATDAAS        → thread_id
  1efb84c2-...       → checkpoint_id
  0                  → 写操作的序号（同一个 checkpoint 可能有多条）
  agent              → 写操作来自哪个 LangGraph 节点（如 agent、tools）
```

## **checkpoint 里存了什么**

点开一个 `checkpoint$...` key，看到的是 JSON 结构：

```json
{
  "v": 1,
  "id": "1efb84c2-c7a2-6fb0-bffc-087a50b63ff8",
  "ts": "2025-04-14T10:23:45.123456+00:00",
  "channel_values": {
    "messages": [
      {
        "type": "human",
        "content": "你好，我叫张晓晓",
        "id": "msg-uuid-001"
      },
      {
        "type": "ai",
        "content": "你好，张晓晓！很高兴认识你，有什么我可以帮助你的吗？",
        "id": "msg-uuid-002"
      }
    ],
    "agent": "__start__"
  },
  "channel_versions": {
    "messages": "1efb84c2...",
    "agent": "..."
  },
  "versions_seen": { ... },
  "pending_sends": []
}
```

| 字段                    | 含义                                                        |
| ----------------------- | ----------------------------------------------------------- |
| id                      | 本次检查点的唯一 ID                                         |
| ts                      | 创建时间戳                                                  |
| channel_values.messages | 这就是对话历史！ 完整的消息列表，Human/AI/Tool 消息全在这里 |
| channel_values.agent    | 当前 LangGraph 图所在的节点名称                             |
| channel_versions        | 各 channel 的版本号，用于增量更新判断                       |

## **checkpoint_write 里存了什么**

`checkpoint_write$...` 是**写操作日志**，记录 Agent 在某个节点执行后产生的新消息：

```json
{
  "v": 1,
  "channel": "messages",
  "type": "msgpack",
  "value": "<二进制序列化的 AIMessage>"
}
```

- 每次 LLM 生成一条回复、每次工具调用，都会产生一条 `checkpoint_write` 记录
- LangGraph 用这些日志来"重建"完整的 checkpoint，类似数据库的 WAL（Write-Ahead Log）

## **用一次对话追踪 Redis 变化**

以下面的测试为例：

```python
# 第一轮
await chat_endpoint("1123", "ATDAAS", "你好，我叫张晓晓")
# 第二轮
await chat_endpoint("1123", "ATDAAS", "我是谁？")
```

Redis 中 key 的变化过程：

```plain text
第一轮对话完成后：
┌─────────────────────────────────────────────────────────────────┐
│ checkpoint$1123:ATDAAS$<uuid-A>$                                │
│   → messages: [HumanMessage("你好，我叫张晓晓"), AIMessage("你好...")]│
│                                                                  │
│ checkpoint_write$1123:ATDAAS$<uuid-A>$0$agent                   │
│   → 记录 AIMessage 写入操作                                       │
└─────────────────────────────────────────────────────────────────┘

第二轮对话完成后，新增：
┌─────────────────────────────────────────────────────────────────┐
│ checkpoint$1123:ATDAAS$<uuid-B>$<uuid-A>     ← 父ID是上一轮的   │
│   → messages: [                                                  │
│       HumanMessage("你好，我叫张晓晓"),                           │
│       AIMessage("你好..."),                                      │
│       HumanMessage("我是谁？"),          ← 新增                  │
│       AIMessage("你叫张晓晓。")          ← 新增                  │
│     ]                                                            │
│                                                                  │
│ checkpoint_write$1123:ATDAAS$<uuid-B>$0$agent                   │
└─────────────────────────────────────────────────────────────────┘
```

**规律总结**：每轮对话后，`messages` 列表是**追加累积**的，新的 checkpoint 包含所有历史消息，并通过 `parent_id` 链接上一个 checkpoint，形成链式结构。

## **Checkpointer 工作时序图**

下图展示两轮对话中，LangGraph Agent 如何通过 `AsyncRedisSaver` 读写 Redis，实现跨轮次记忆。

1. 和大模型对话之前。LangGraph 自动提取 短期记忆中的消息记录。拼接消息列表，发给大模型
2. 和大模型对话之后。LangGraph 主动更新 短期记忆中的消息记录。并把这次AI消息，返回给用户