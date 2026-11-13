# 分层架构的守卫测试。
#
# 这些断言不测业务逻辑，只锁住依赖方向。一旦有人重新引入反向依赖，
# 单测立刻失败，而不是等到线上出现"导入顺序变了就崩"这类怪问题。

from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"


def _read(*parts: str) -> str:
    return (SRC.joinpath(*parts)).read_text(encoding="utf-8")


def test_tools_layer_never_imports_agents():
    """tools 是最底层的领域工具，不能反向依赖 agents。"""
    for path in sorted((SRC / "tools").glob("*.py")):
        assert "from src.agents" not in path.read_text(encoding="utf-8"), (
            f"{path.name} 反向依赖了 agents：依赖方向会被破坏"
        )


def test_core_layer_never_imports_upper_layers():
    """core 是最底层，不能依赖 infra / modules / agents / api。"""
    for layer in ("infra", "modules", "agents", "api"):
        for path in sorted((SRC / "core").glob("*.py")):
            assert f"from src.{layer}" not in path.read_text(encoding="utf-8"), (
                f"core/{path.name} 反向依赖了 {layer}"
            )


@pytest.mark.parametrize(("module", "upper"), [
    ("context", "agents"),
    ("persist_queue", "modules"),
    ("persist_queue", "infra"),
])
def test_core_modules_stay_decoupled(module: str, upper: str):
    assert f"from src.{upper}" not in _read("core", f"{module}.py")


def test_user_context_lives_in_core():
    """UserContext 被 api / agents / tools 共用，只能放在最底层。"""
    assert "class UserContext" in _read("core", "context.py")


def test_persist_writer_lives_in_modules():
    """落库实现必须留在知道表模型的那一层。"""
    assert "async def write_batch" in _read("modules", "persist_writer.py")


def test_inquiry_graph_only_orchestrates():
    """graph.py 只保留编排：节点与依赖容器都不能再塞回去。"""
    graph = _read("agents", "inquiry", "graph.py")
    assert "async def node_load_context" not in graph
    assert "class InquiryDeps" not in graph

    assert "async def node_load_context" in _read("agents", "inquiry", "nodes.py")
    assert "class InquiryDeps" in _read("agents", "inquiry", "deps.py")


def test_api_layer_never_raises_bare_http_exception():
    """错误必须带业务码：裸 HTTPException 的 body 是 {"detail":...}，两种格式并存过。"""
    for path in sorted((SRC / "api").rglob("*.py")):
        assert "raise HTTPException" not in path.read_text(encoding="utf-8"), (
            f"{path.relative_to(SRC)} 抛出了裸 HTTPException"
        )


def test_sse_helpers_live_in_their_own_module():
    """SSE 帧与 seq 逻辑独立成模块，才能脱离 chat 路由单测。"""
    assert "class SseWriter" in _read("api", "routers", "sse.py")
    assert "class SseWriter" not in _read("api", "routers", "chat.py")
