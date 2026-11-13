from langchain_core.tools import tool
from langgraph.prebuilt import ToolRuntime

def get_user_id(runtime: ToolRuntime):
    # 从 thread_id 中提取用户ID
    if runtime.config.get("configurable"):
        thread_id = runtime.config.get("configurable").get("thread_id")
        if thread_id:
            return thread_id.split(":")[0]
    return None


def is_anonymous(runtime: ToolRuntime) -> bool:
    """游客判定：优先看 UserContext.authenticated，回退看 user_id 是否为 anon 占位符。"""
    ctx = getattr(runtime, "context", None)
    if ctx is not None and hasattr(ctx, "authenticated"):
        return not bool(ctx.authenticated)
    uid = get_user_id(runtime)
    return (not uid) or uid == "anon"


SIGN_IN_HINT = "当前为未登录（游客）状态，长期记忆功能不可用；请提示用户登录后使用。"
# Milvus 不可用时 store 为 None：给出可继续对话的降级提示，不能抛异常中断整轮
STORE_UNAVAILABLE_HINT = "长期记忆服务暂时不可用，本次不保存/检索记忆，请继续正常回答用户。"


def _store_of(runtime: ToolRuntime):
    """取长期记忆 store；Milvus 不可用时为 None（见 supervisor_agent 的降级逻辑）。"""
    return getattr(runtime, "store", None)


@tool
async def save_memory(
    content: str,
    runtime: ToolRuntime,
) -> str:
    """
    将重要信息保存到长期记忆中。
    当用户提到重要的个人信息、偏好、病史、过敏史、药物史、手术史等需要跨会话记住的内容时调用。

    Args:
        content: 要记住的内容，用一句话描述
    """

    # 记忆内容可能含病史等 PHI，日志只记长度
    print(f"🔧执行：保存长期记忆 chars={len(content)}")
    if is_anonymous(runtime):
        print("⚠️ 游客模式：拒绝保存长期记忆")
        return SIGN_IN_HINT
    user_id = get_user_id(runtime)
    if not user_id:
        return "无法获取用户ID。"
    store = _store_of(runtime)
    if store is None:
        return STORE_UNAVAILABLE_HINT

    import time, uuid
    # 加 uuid 后缀：同一秒内多次保存时避免 key 冲突导致记忆被覆盖
    key = f"memory_{int(time.time())}_{uuid.uuid4().hex[:8]}"
    await store.aput(
        namespace=("users", user_id, "memories"),
        key=key,
        value={"content": content, "timestamp": time.time()},
    )
    return f"已记住：{content}"


@tool
async def search_memory(
    query: str,
    runtime: ToolRuntime,
) -> str:
    """
    从长期记忆中检索与问题相关的历史信息。
    当需要回忆用户之前说过的内容、历史病情、偏好等时调用。

    Args:
        query: 检索关键词或问题
    """

    user_id = get_user_id(runtime)
    print(f"🔧执行：搜索长期记忆 chars={len(query)}; 用户id：{user_id}")
    if is_anonymous(runtime):
        return "当前为未登录（游客）状态，没有可检索的长期记忆。"
    if not user_id:
        return "无法获取用户ID。"
    store = _store_of(runtime)
    if store is None:
        return STORE_UNAVAILABLE_HINT

    results = await store.asearch(
        ("users", user_id, "memories"),
        query=query,
        limit=5,
    )
    if not results:
        return "没有找到相关记忆。"

    memories = [f"- {item.value['content']}" for item in results]
    return "相关历史记忆：\n" + "\n".join(memories)