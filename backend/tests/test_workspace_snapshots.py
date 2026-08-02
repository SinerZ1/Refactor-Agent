from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

import agent.tools as agent_tools
import agent.workspace as agent_workspace
from agent.state import State, compute_change_set_digest, review_evidence_errors


@pytest.fixture
def snapshot_project(tmp_path, monkeypatch):
    code_root = tmp_path / "CodeSmells"
    code_root.mkdir()
    (code_root / "example.py").write_text("value = 'baseline'\n", encoding="utf-8")
    monkeypatch.setattr(agent_tools, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(agent_tools, "REFACTOR_ROOT", code_root)
    monkeypatch.setattr(agent_workspace, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(agent_workspace, "REFACTOR_ROOT", code_root)
    monkeypatch.setattr(
        agent_workspace, "WORKSPACE_ROOT", tmp_path / ".refactor-workspaces"
    )
    state = agent_workspace.create_run_workspace()
    yield state
    workspace_dir = agent_workspace.WORKSPACE_ROOT / state["workspace_id"]
    if workspace_dir.exists():
        agent_workspace.cleanup_run_workspace(state["workspace_id"])


def _change_record(path: str, before: str, after: str):
    return {
        "file_path": path,
        "before_sha256": before,
        "after_sha256": after,
        "added_lines": 1,
        "removed_lines": 1,
        "unified_diff": "-before\n+after",
        "diff_truncated": False,
    }


def _invoke_tests(state, runner, *, suite="codesmells"):
    tool_call = {
        "name": "run_unit_tests",
        "args": {"test_suite": suite},
        "id": "snapshot-test-call",
        "type": "tool_call",
    }
    graph_builder = StateGraph(State)
    graph_builder.add_node("tools", ToolNode([agent_tools.run_unit_tests]))
    graph_builder.add_edge(START, "tools")
    graph_builder.add_edge("tools", END)
    with patch.object(agent_tools.subprocess, "run", runner):
        return graph_builder.compile().invoke(
            {
                **state,
                "messages": [AIMessage(content="", tool_calls=[tool_call])],
                "retry_count": 0,
                "test_run_records": state.get("test_run_records", []),
            }
        )


def _passing_runner(*_args, **_kwargs):
    return SimpleNamespace(stdout="tests passed", stderr="", returncode=0)


def _attach_review_evidence(state):
    latest = state["test_run_records"][-1]
    state["review_evidence"] = {
        "changed_files": agent_workspace.workspace_changed_paths(state["workspace_id"]),
        "change_set_digest": compute_change_set_digest(state["change_records"]),
        "workspace_snapshot_digest": latest["workspace_snapshot_digest"],
        "test_suite": latest["suite"],
        "test_success": latest["success"],
        "test_exit_code": latest["exit_code"],
    }


def test_snapshot_binds_files_evicted_from_change_history(snapshot_project):
    working_root = (
        agent_workspace.WORKSPACE_ROOT
        / snapshot_project["workspace_id"]
        / "working"
        / "CodeSmells"
    )
    records = []
    for index in range(15):
        (working_root / f"file_{index}.py").write_text(
            f"value = {index}\n", encoding="utf-8"
        )
        records.append(
            _change_record(f"CodeSmells/file_{index}.py", "missing", str(index))
        )
    snapshot_project["change_records"] = records[-12:]

    result = _invoke_tests(snapshot_project, _passing_runner)
    snapshot = agent_workspace.build_workspace_snapshot(
        snapshot_project["workspace_id"]
    )

    assert len(result["change_records"]) == 12
    assert any(item["path"] == "CodeSmells/file_0.py" for item in snapshot["files"])
    assert (
        result["test_run_records"][-1]["workspace_snapshot_digest"]
        == snapshot["digest"]
    )


def test_snapshot_binds_final_content_after_repeated_writes(snapshot_project):
    target = agent_workspace.resolve_workspace_path(
        snapshot_project["workspace_id"], "CodeSmells/example.py"
    )
    target.write_text("value = 'first'\n", encoding="utf-8")
    target.write_text("value = 'final'\n", encoding="utf-8")
    snapshot_project["change_records"] = [
        _change_record("CodeSmells/example.py", "baseline", "first"),
        _change_record("CodeSmells/example.py", "first", "final"),
    ]

    result = _invoke_tests(snapshot_project, _passing_runner)
    snapshot = agent_workspace.build_workspace_snapshot(
        snapshot_project["workspace_id"]
    )
    example = next(
        item for item in snapshot["files"] if item["path"] == "CodeSmells/example.py"
    )

    assert target.read_text(encoding="utf-8") == "value = 'final'\n"
    assert example["sha256"] == agent_workspace._sha256_bytes(target.read_bytes())
    assert (
        result["test_run_records"][-1]["workspace_snapshot_digest"]
        == snapshot["digest"]
    )


def test_source_mutation_by_passing_test_invalidates_evidence(snapshot_project):
    snapshot_project["change_records"] = [
        _change_record("CodeSmells/example.py", "baseline", "candidate")
    ]

    def mutating_runner(*_args, **kwargs):
        Path(kwargs["cwd"], "CodeSmells", "example.py").write_text(
            "value = 'mutated-by-test'\n", encoding="utf-8"
        )
        return SimpleNamespace(stdout="all assertions passed", stderr="", returncode=0)

    result = _invoke_tests(snapshot_project, mutating_runner)
    record = result["test_run_records"][-1]

    assert record["exit_code"] == 0
    assert record["success"] is False
    assert record["workspace_stable"] is False
    assert record["failure_kind"] == "workspace_mutated_by_tests"
    assert record["success_marker_present"] is False


def test_explicit_cache_outputs_do_not_change_snapshot(snapshot_project):
    snapshot_project["change_records"] = [
        _change_record("CodeSmells/example.py", "baseline", "candidate")
    ]

    def cache_runner(*_args, **kwargs):
        code_root = Path(kwargs["cwd"], "CodeSmells")
        bytecode = code_root / "__pycache__" / "example.pyc"
        bytecode.parent.mkdir()
        bytecode.write_bytes(b"cache")
        pytest_cache = code_root / ".pytest_cache" / "state"
        pytest_cache.parent.mkdir()
        pytest_cache.write_text("cache", encoding="utf-8")
        (code_root / ".coverage").write_bytes(b"coverage")
        return SimpleNamespace(stdout="passed", stderr="", returncode=0)

    result = _invoke_tests(snapshot_project, cache_runner)
    record = result["test_run_records"][-1]

    assert record["success"] is True
    assert record["workspace_stable"] is True
    assert record["success_marker_present"] is True


def test_developer_write_clears_and_invalidates_old_snapshot_evidence(
    snapshot_project,
):
    snapshot_project["change_records"] = [
        _change_record("CodeSmells/example.py", "baseline", "candidate")
    ]
    tested = _invoke_tests(snapshot_project, _passing_runner)
    old_record = tested["test_run_records"][-1]
    tool_call = {
        "name": "write_code_file",
        "args": {
            "file_path": "CodeSmells/example.py",
            "content": "value = 'developer-write-after-test'\n",
        },
        "id": "write-after-test",
        "type": "tool_call",
    }
    graph_builder = StateGraph(State)
    graph_builder.add_node("tools", ToolNode([agent_tools.write_code_file]))
    graph_builder.add_edge(START, "tools")
    graph_builder.add_edge("tools", END)
    result = graph_builder.compile().invoke(
        {
            **tested,
            "messages": [AIMessage(content="", tool_calls=[tool_call])],
        }
    )

    assert result["test_run_records"] == []
    stale_state = {**result, "test_run_records": [old_record]}
    assert "当前工作区已偏离成功测试绑定的完整快照" in review_evidence_errors(
        stale_state
    )


def test_external_change_is_rejected_before_final_approval(snapshot_project):
    target = agent_workspace.resolve_workspace_path(
        snapshot_project["workspace_id"], "CodeSmells/example.py"
    )
    target.write_text("value = 'candidate'\n", encoding="utf-8")
    snapshot_project["change_records"] = [
        _change_record("CodeSmells/example.py", "baseline", "candidate")
    ]
    tested = _invoke_tests(snapshot_project, _passing_runner)
    _attach_review_evidence(tested)
    target.write_text("value = 'external'\n", encoding="utf-8")

    result = agent_workspace.prepare_workspace_approval_node(tested)

    assert result["review_status"] == "failed"
    assert "完整快照" in result["workspace_error"]


def test_external_change_after_approval_is_rejected_at_apply(snapshot_project):
    target = agent_workspace.resolve_workspace_path(
        snapshot_project["workspace_id"], "CodeSmells/example.py"
    )
    target.write_text("value = 'candidate'\n", encoding="utf-8")
    snapshot_project["change_records"] = [
        _change_record("CodeSmells/example.py", "baseline", "candidate")
    ]
    tested = _invoke_tests(snapshot_project, _passing_runner)
    _attach_review_evidence(tested)
    tested.update(agent_workspace.prepare_workspace_approval_node(tested))
    target.write_text("value = 'tampered-after-approval'\n", encoding="utf-8")

    with pytest.raises(agent_workspace.WorkspaceError, match="内容已改变|完整工作区"):
        agent_workspace.atomic_apply_workspace(tested)


def test_manifest_digest_is_independent_of_enumeration_order():
    entries = [
        ("CodeSmells/z.py", b"z\n"),
        ("CodeSmells/a.py", b"a\n"),
    ]

    forward = agent_workspace.build_snapshot_manifest(entries)
    reversed_snapshot = agent_workspace.build_snapshot_manifest(reversed(entries))

    assert forward == reversed_snapshot


def test_manifest_normalizes_windows_and_posix_path_expressions():
    windows = agent_workspace.build_snapshot_manifest(
        [("CodeSmells\\nested\\module.py", b"value = 1\n")]
    )
    posix = agent_workspace.build_snapshot_manifest(
        [("CodeSmells/nested/module.py", b"value = 1\n")]
    )

    assert windows["digest"] == posix["digest"]
    assert windows["files"][0]["path"] == "CodeSmells/nested/module.py"
