from langchain_core.messages import AIMessage

from agent.edges import route_architect, route_developer, route_reviewer
from agent.nodes import (
    finalize_budget_failure_node,
    finalize_review_failure_node,
    finalize_review_success_node,
    reviewer_protocol_retry_node,
)
from agent.state import ChangeRecord
from agent.state import TestRunRecord as StructuredTestRunRecord
from agent.state import compute_change_set_digest


def _change_record(file_path: str = "CodeSmells/example.py") -> ChangeRecord:
    return {
        "file_path": file_path,
        "before_sha256": f"before:{file_path}",
        "after_sha256": f"after:{file_path}",
        "added_lines": 2,
        "removed_lines": 1,
        "unified_diff": "-old\n+new",
        "diff_truncated": False,
    }


def _test_record(
    change_records: list[ChangeRecord],
    *,
    suite: str = "CodeSmells",
    success: bool = True,
    exit_code: int = 0,
) -> StructuredTestRunRecord:
    return {
        "suite": suite,
        "success": success,
        "exit_code": exit_code,
        "change_set_digest": compute_change_set_digest(change_records),
        "output_excerpt": "tests passed" if success else "tests failed",
    }


def _state(
    content: str,
    *,
    retries: int = 0,
    protocol_errors: int = 0,
    changes: list[ChangeRecord] | None = None,
    tests: list[StructuredTestRunRecord] | None = None,
):
    return {
        "messages": [AIMessage(content=content)],
        "retry_count": retries,
        "review_protocol_errors": protocol_errors,
        "review_status": "running",
        "change_records": changes or [],
        "test_run_records": tests or [],
    }


def test_success_claim_without_write_record_enters_protocol_correction():
    assert (
        route_reviewer(_state("【REFACTOR_SUCCESS】测试通过"))
        == "reviewer_protocol_retry"
    )


def test_success_claim_without_test_run_enters_protocol_correction():
    changes = [_change_record()]

    assert (
        route_reviewer(_state("【REFACTOR_SUCCESS】", changes=changes))
        == "reviewer_protocol_retry"
    )


def test_success_claim_with_failed_test_enters_protocol_correction():
    changes = [_change_record()]
    failed_test = _test_record(changes, success=False, exit_code=1)

    assert (
        route_reviewer(
            _state("【REFACTOR_SUCCESS】", changes=changes, tests=[failed_test])
        )
        == "reviewer_protocol_retry"
    )


def test_new_write_invalidates_previous_passing_test():
    first_change = _change_record()
    passing_test = _test_record([first_change])
    latest_changes = [first_change, _change_record()]

    assert (
        route_reviewer(
            _state(
                "【REFACTOR_SUCCESS】",
                changes=latest_changes,
                tests=[passing_test],
            )
        )
        == "reviewer_protocol_retry"
    )


def test_current_codesmells_test_and_success_marker_reach_success_terminal():
    changes = [_change_record()]
    passing_test = _test_record(changes, suite="backend/tests + CodeSmells")
    state = _state(
        "【REFACTOR_SUCCESS】测试通过", changes=changes, tests=[passing_test]
    )

    assert route_reviewer(state) == "finalize_review_success"
    terminal = finalize_review_success_node(state)
    assert terminal["review_status"] == "success"
    assert terminal["review_evidence"] == {
        "changed_files": ["CodeSmells/example.py"],
        "change_set_digest": compute_change_set_digest(changes),
        "test_suite": "backend/tests + CodeSmells",
        "test_success": True,
        "test_exit_code": 0,
    }


def test_backend_only_test_cannot_satisfy_codesmells_gate():
    changes = [_change_record()]
    passing_test = _test_record(changes, suite="backend/tests")

    assert (
        route_reviewer(
            _state("【REFACTOR_SUCCESS】", changes=changes, tests=[passing_test])
        )
        == "reviewer_protocol_retry"
    )


def test_success_terminal_defensively_rechecks_evidence():
    terminal = finalize_review_success_node(_state("【REFACTOR_SUCCESS】"))

    assert terminal["review_status"] == "failed"
    assert terminal["review_evidence"] is None
    assert "【REFACTOR_FAIL】" in terminal["messages"][0].content


def test_reviewer_failure_retries_developer_at_most_three_times():
    assert route_reviewer(_state("【REFACTOR_FAIL】测试失败")) == "developer_retry"
    exhausted_state = _state("【REFACTOR_FAIL】仍然失败", retries=3)
    assert route_reviewer(exhausted_state) == "finalize_review_failure"

    terminal = finalize_review_failure_node(exhausted_state)
    assert terminal["review_status"] == "failed"
    assert "最多 3 次重试" in terminal["messages"][0].content


def test_reviewer_protocol_errors_are_bounded():
    assert (
        route_reviewer(_state("缺少标记", protocol_errors=1))
        == "reviewer_protocol_retry"
    )
    assert (
        route_reviewer(_state("仍缺少标记", protocol_errors=2))
        == "finalize_review_failure"
    )


def test_protocol_retry_node_increments_counter_and_explains_evidence_gap():
    result = reviewer_protocol_retry_node(_state("【REFACTOR_SUCCESS】"))

    assert result["review_protocol_errors"] == 1
    assert "没有成功写入记录" in result["messages"][0].content
    assert "run_unit_tests" in result["messages"][0].content
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
