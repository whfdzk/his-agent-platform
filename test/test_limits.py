"""执行限额：Agent/工具/Token/检索条数/时间。"""

import pytest

from src.core.limits import (
    BudgetExceeded,
    ExecutionBudget,
    clamp_retrieval,
    current_budget,
    reset_budget,
    start_budget,
)


def test_tool_call_budget_raises():
    budget = ExecutionBudget(max_tool_calls=2)
    budget.consume_tool("a")
    budget.consume_tool("b")
    with pytest.raises(BudgetExceeded) as exc:
        budget.consume_tool("c")
    assert exc.value.kind == "tool_calls_exceeded"


def test_agent_call_budget_raises():
    budget = ExecutionBudget(max_agent_calls=1)
    budget.consume_agent("supervisor")
    with pytest.raises(BudgetExceeded) as exc:
        budget.consume_agent("inquiry")
    assert exc.value.kind == "agent_calls_exceeded"


def test_token_budget_raises():
    budget = ExecutionBudget(max_tokens=100)
    budget.add_tokens(80)
    budget.ensure_tokens()
    budget.add_tokens(30)
    with pytest.raises(BudgetExceeded) as exc:
        budget.ensure_tokens()
    assert exc.value.kind == "token_budget_exceeded"


def test_retrieval_is_clamped():
    budget = ExecutionBudget(max_retrieval_results=5)
    assert budget.clamp_retrieval(50) == 5
    assert budget.clamp_retrieval(None) == 5
    assert budget.clamp_retrieval(2) == 2


def test_time_budget_raises():
    budget = ExecutionBudget(max_seconds=0)
    with pytest.raises(BudgetExceeded) as exc:
        budget.check_time()
    assert exc.value.kind == "time_budget_exceeded"


def test_context_budget_lifecycle():
    assert current_budget() is None
    budget, token = start_budget()
    assert current_budget() is budget
    assert clamp_retrieval(999) <= budget.max_retrieval_results
    reset_budget(token)
    assert current_budget() is None
