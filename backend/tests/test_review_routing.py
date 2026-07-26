from langchain_core.messages import AIMessage

from agent.edges import route_architect, route_developer, route_reviewer
from agent.nodes import (
    finalize_budget_failure_node,
    finalize_review_failure_node,
    finalize_review_success_node,
    reviewer_protocol_retry_node,
)


def _state(content: str, *, retries: int = 0, protocol_errors: int = 0):
    return {
        "messages": [AIMessage(content=content)],
        "retry_count": retries,
        "review_protocol_errors": protocol_errors,
        "review_status": "running",
    }


def test_reviewer_only_succeeds_with_explicit_success_marker():
    assert route_reviewer(_state("实现看起来不错")) == "reviewer_protocol_retry"
    assert (
        route_reviewer(_state("【REFACTOR_SUCCESS】测试通过"))
        == "finalize_review_success"
    )


def test_reviewer_failure_retries_developer_at_most_three_times():
    assert route_reviewer(_state("【REFACTOR_FAIL】测试失败")) == "developer_retry"
    assert (
        route_reviewer(_state("【REFACTOR_FAIL】仍然失败", retries=3))
        == "finalize_review_failure"
    )


def test_reviewer_protocol_errors_are_bounded():
    assert (
        route_reviewer(_state("缺少标记", protocol_errors=1))
        == "reviewer_protocol_retry"
    )
    assert (
        route_reviewer(_state("仍缺少标记", protocol_errors=2))
        == "finalize_review_failure"
    )


def test_review_terminal_nodes_write_machine_readable_status():
    assert finalize_review_success_node(_state("【REFACTOR_SUCCESS】")) == {
        "review_status": "success"
    }

    failure = finalize_review_failure_node(_state("没有协议标记", protocol_errors=2))
    assert failure["review_status"] == "failed"
    assert "【REFACTOR_FAIL】" in failure["messages"][0].content


def test_protocol_retry_node_increments_counter_and_explains_contract():
    result = reviewer_protocol_retry_node(_state("缺少标记", protocol_errors=1))
    assert result["review_protocol_errors"] == 2
    assert "【REFACTOR_SUCCESS】" in result["messages"][0].content
    assert "【REFACTOR_FAIL】" in result["messages"][0].content


def test_budget_failure_has_priority_in_every_role_route():
    state = _state("看似正常")
    state["budget_exceeded"] = True

    assert route_architect(state) == "finalize_budget_failure"
    assert route_developer(state) == "finalize_budget_failure"
    assert route_reviewer(state) == "finalize_budget_failure"

    terminal = finalize_budget_failure_node(
        {**state, "budget_reason": "工具调用预算耗尽"}
    )
    assert terminal["review_status"] == "failed"
    assert "【REFACTOR_FAIL】" in terminal["messages"][0].content
    assert "工具调用预算耗尽" in terminal["messages"][0].content
