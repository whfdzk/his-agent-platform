from langchain_core.tools import tool
from langgraph.prebuilt import ToolRuntime

from src.core.memory_schema import infer_kind, label_of, resolve_kind

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
    kind: str = "other",
) -> str:
    """
    将重要信息保存到长期记忆中。
    当用户提到重要的个人信息、偏好、病史、过敏史、药物史、手术史等需要跨会话记住的内容时调用。

    Args:
        content: 要记住的内容，用一句话描述事实本身，例如"我对青霉素过敏，吃阿莫西林起过皮疹"
        kind: 记忆类别，只能取以下四个值之一：
            allergy = 过敏史（药物/食物过敏、用药后的不良反应）
            condition = 疾病史（慢性病、确诊疾病、手术史）
            medication = 用药史（正在长期服用或近期用过的药）
            other = 其他（偏好、生活习惯、一般个人信息）
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

    import re, time, uuid
    from difflib import SequenceMatcher

    ns = ("users", user_id, "memories")

    def _norm(t: str) -> str:
        return re.sub(r"[\s，。、；：！？,.!?;:（）()【】\[\]]", "", t or "")

    # 写入前去重：用户可能反复说同一件事（实测连续两轮说"我青霉素过敏"会存成两条）。
    # 不能只看向量分数：实测同一句话的 COSINE 分数只有 0.876（embedding 有轻微非确定性），
    # 用 0.95 的分数阈值等于不生效。所以先用检索取语义近邻，再比对文本本身。
    try:
        dup = await store.asearch(ns, query=content, limit=3)
        new_norm = _norm(content)
        for d in dup:
            old = _norm((d.value or {}).get("content", ""))
            if not old:
                continue
            if old == new_norm or SequenceMatcher(None, old, new_norm).ratio() >= 0.9:
                return f"这条信息已经在长期记忆里了，无需重复记录：{content}"
    except Exception as e:
        # 判重失败不影响写入，继续往下存
        print(f"⚠️ 记忆判重失败，按新记忆写入: {type(e).__name__}")

    # 加 uuid 后缀：同一秒内多次保存时避免 key 冲突导致记忆被覆盖
    key = f"memory_{int(time.time())}_{uuid.uuid4().hex[:8]}"
    # 结构化：显式存 kind，模型没给或给错时用关键词规则兜底
    resolved_kind = resolve_kind(content, kind)
    await store.aput(
        namespace=ns,
        key=key,
        value={"content": content, "kind": resolved_kind, "timestamp": time.time()},
    )
    return f"已记住（{label_of(resolved_kind)}）：{content}"


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

    memories = []
    for item in results:
        v = item.value or {}
        content = (v.get("content") or "").strip()
        if not content:
            continue
        # 老记忆没有 kind 字段，用规则推断，保证输出始终带类别
        kind = resolve_kind(content, v.get("kind"))
        memories.append(f"- [{label_of(kind)}] {content}")
    if not memories:
        return "没有找到相关记忆。"
    return "相关历史记忆：\n" + "\n".join(memories)