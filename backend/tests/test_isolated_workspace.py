from pathlib import Path

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode
from langgraph.types import Command

import agent.tools as agent_tools
import agent.workspace as agent_workspace
from agent.state import State


@pytest.fixture
def isolated_project(tmp_path, monkeypatch):
    code_root = tmp_path / "CodeSmells"
    code_root.mkdir()
    monkeypatch.setattr(agent_tools, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(agent_tools, "REFACTOR_ROOT", code_root)
    monkeypatch.setattr(agent_workspace, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(agent_workspace, "REFACTOR_ROOT", code_root)
    monkeypatch.setattr(
        agent_workspace, "WORKSPACE_ROOT", tmp_path / ".refactor-workspaces"
    )
    return tmp_path, code_root


def _workspace_with_changes(
    code_root: Path,
    changes: dict[str, tuple[str, str]],
) -> dict:
    for relative, (before, _after) in changes.items():
        target = code_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(before, encoding="utf-8")
    state = agent_workspace.create_run_workspace()
    for relative, (_before, after) in changes.items():
        target = agent_workspace.resolve_workspace_path(
            state["workspace_id"], f"CodeSmells/{relative}"
        )
        target.write_text(after, encoding="utf-8")
    prepared = agent_workspace.prepare_workspace_approval_node(state)
    state.update(prepared)
    return state


def test_review_failure_never_modifies_real_source(isolated_project):
    _project_root, code_root = isolated_project
    state = _workspace_with_changes(
        code_root, {"example.py": ("original\n", "reviewed-but-failed\n")}
    )

    cleanup = agent_workspace.cleanup_workspace_node(state)

    assert cleanup["workspace_cleaned"] is True
    assert (code_root / "example.py").read_text(encoding="utf-8") == "original\n"
    assert not (agent_workspace.WORKSPACE_ROOT / state["workspace_id"]).exists()


def test_user_rejection_discards_aggregate_diff(isolated_project):
    _project_root, code_root = isolated_project
    state = _workspace_with_changes(
        code_root, {"example.py": ("original\n", "candidate\n")}
    )
    graph_builder = StateGraph(State)
    graph_builder.add_node("apply", agent_workspace.apply_workspace_changes_node)
    graph_builder.add_edge(START, "apply")
    graph_builder.add_edge("apply", END)
    graph = graph_builder.compile(checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": "reject-final-diff"}}

    graph.invoke(state, config)
    suspended = graph.get_state(config)
    assert suspended.interrupts
    payload = suspended.interrupts[0].value
    assert payload["type"] == "aggregate_diff_approval"
    assert payload["changed_files"] == ["CodeSmells/example.py"]
    assert "-original" in payload["aggregate_diff"]
    assert "+candidate" in payload["aggregate_diff"]

    result = graph.invoke(Command(resume={"approved": False}), config)

    assert result["review_status"] == "failed"
    assert result["workspace_approved"] is False
    assert result["workspace_applied"] is False
    assert result["workspace_cleaned"] is True
    assert (code_root / "example.py").read_text(encoding="utf-8") == "original\n"


def test_multi_file_apply_failure_rolls_back_every_replaced_file(
    isolated_project, monkeypatch
):
    _project_root, code_root = isolated_project
    state = _workspace_with_changes(
        code_root,
        {
            "first.py": ("first-old\n", "first-new\n"),
            "second.py": ("second-old\n", "second-new\n"),
        },
    )
    real_replace = agent_workspace.os.replace
    replace_calls = 0

    def fail_second_commit(source, destination):
        nonlocal replace_calls
        replace_calls += 1
        if replace_calls == 2:
            raise PermissionError("simulated Windows sharing violation")
        return real_replace(source, destination)

    monkeypatch.setattr(agent_workspace.os, "replace", fail_second_commit)

    with pytest.raises(agent_workspace.AtomicApplyError) as failure:
        agent_workspace.atomic_apply_workspace(state)

    assert failure.value.rolled_back is True
    assert (code_root / "first.py").read_text(encoding="utf-8") == "first-old\n"
    assert (code_root / "second.py").read_text(encoding="utf-8") == "second-old\n"


def test_external_baseline_change_is_never_overwritten(isolated_project):
    _project_root, code_root = isolated_project
    state = _workspace_with_changes(
        code_root, {"example.py": ("baseline\n", "candidate\n")}
    )
    (code_root / "example.py").write_text("external-edit\n", encoding="utf-8")

    with pytest.raises(agent_workspace.BaselineConflictError):
        agent_workspace.atomic_apply_workspace(state)

    assert (code_root / "example.py").read_text(encoding="utf-8") == "external-edit\n"


def test_workspace_change_after_diff_approval_is_rejected(isolated_project):
    _project_root, code_root = isolated_project
    state = _workspace_with_changes(
        code_root, {"example.py": ("baseline\n", "approved-candidate\n")}
    )
    agent_workspace.resolve_workspace_path(
        state["workspace_id"], "CodeSmells/example.py"
    ).write_text("changed-after-approval\n", encoding="utf-8")

    with pytest.raises(agent_workspace.WorkspaceError):
        agent_workspace.atomic_apply_workspace(state)

    assert (code_root / "example.py").read_text(encoding="utf-8") == "baseline\n"


def test_aggregate_diff_exposes_line_ending_only_change(isolated_project):
    _project_root, code_root = isolated_project
    target = code_root / "example.py"
    target.write_bytes(b"same-content\r\n")
    state = agent_workspace.create_run_workspace()
    agent_workspace.resolve_workspace_path(
        state["workspace_id"], "CodeSmells/example.py"
    ).write_bytes(b"same-content\n")

    aggregate_diff, _hashes, changed_files = agent_workspace.build_aggregate_diff(
        state["workspace_id"]
    )

    assert changed_files == ["CodeSmells/example.py"]
    assert "仅换行符或文件结尾发生变化" in aggregate_diff


def test_cleanup_rejects_workspace_escape(isolated_project):
    project_root, _code_root = isolated_project
    sentinel = project_root / "sentinel.txt"
    sentinel.write_text("keep", encoding="utf-8")

    with pytest.raises(agent_workspace.WorkspaceError):
        agent_workspace.cleanup_run_workspace("../")

    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_successful_atomic_apply_records_hashes_and_cleans_resources(
    isolated_project,
):
    _project_root, code_root = isolated_project
    state = _workspace_with_changes(
        code_root,
        {
            "first.py": ("first-old\n", "first-new\n"),
            "nested/second.py": ("second-old\n", "second-new\n"),
        },
    )
    graph_builder = StateGraph(State)
    graph_builder.add_node("apply", agent_workspace.apply_workspace_changes_node)
    graph_builder.add_edge(START, "apply")
    graph_builder.add_edge("apply", END)
    graph = graph_builder.compile(checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": "approve-final-diff"}}
    graph.invoke(state, config)

    result = graph.invoke(Command(resume={"approved": True}), config)

    assert result["workspace_approved"] is True
    assert result["workspace_applied"] is True
    assert result["workspace_rolled_back"] is False
    assert result["workspace_cleaned"] is True
    assert result["baseline_file_hashes"]
    assert result["final_file_hashes"]
    assert result["aggregate_diff"]
    assert (code_root / "first.py").read_text(encoding="utf-8") == "first-new\n"
    assert (code_root / "nested" / "second.py").read_text(
        encoding="utf-8"
    ) == "second-new\n"
    assert not (agent_workspace.WORKSPACE_ROOT / state["workspace_id"]).exists()
    assert not list(code_root.rglob("*.prepared-*"))
    assert not list(code_root.rglob("*.backup-*"))


def test_reviewer_command_targets_working_snapshot(isolated_project, monkeypatch):
    _project_root, code_root = isolated_project
    (code_root / "example.py").write_text("baseline\n", encoding="utf-8")
    workspace_state = agent_workspace.create_run_workspace()
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["cwd"] = kwargs["cwd"]
        captured["env"] = kwargs["env"]
        return type("Result", (), {"stdout": "passed", "stderr": "", "returncode": 0})()

    monkeypatch.setattr(agent_tools.subprocess, "run", fake_run)
    tool_call = {
        "name": "run_unit_tests",
        "args": {"test_suite": "codesmells"},
        "id": "tests-in-workspace",
        "type": "tool_call",
    }
    graph_builder = StateGraph(State)
    graph_builder.add_node("tools", ToolNode([agent_tools.run_unit_tests]))
    graph_builder.add_edge(START, "tools")
    graph_builder.add_edge("tools", END)
    result = graph_builder.compile().invoke(
        {
            "messages": [AIMessage(content="", tool_calls=[tool_call])],
            "retry_count": 0,
            "change_records": [],
            "test_run_records": [],
            **workspace_state,
        }
    )
    working_root = (
        agent_workspace.WORKSPACE_ROOT / workspace_state["workspace_id"] / "working"
    )

    assert result["test_run_records"], result["messages"][-1]
    assert result["test_run_records"][-1]["success"] is True
    assert captured["cwd"] == str(working_root)
    assert str(working_root / "CodeSmells") in captured["command"]
    assert captured["env"]["PYTHONPATH"].split(agent_tools.os.pathsep)[0] == str(
        working_root
    )
    assert captured["env"]["PYTHONDONTWRITEBYTECODE"] == "1"


def test_runtime_bytecode_is_excluded_from_final_diff(isolated_project):
    _project_root, code_root = isolated_project
    (code_root / "example.py").write_text("baseline\n", encoding="utf-8")
    state = agent_workspace.create_run_workspace()
    agent_workspace.resolve_workspace_path(
        state["workspace_id"], "CodeSmells/example.py"
    ).write_text("candidate\n", encoding="utf-8")
    cache_file = (
        agent_workspace.WORKSPACE_ROOT
        / state["workspace_id"]
        / "working"
        / "CodeSmells"
        / "__pycache__"
        / "example.cpython-312.pyc"
    )
    cache_file.parent.mkdir()
    cache_file.write_bytes(b"\x00\x01runtime-only")

    aggregate_diff, final_hashes, changed_files = agent_workspace.build_aggregate_diff(
        state["workspace_id"]
    )

    assert changed_files == ["CodeSmells/example.py"]
    assert "__pycache__" not in aggregate_diff
    assert all("__pycache__" not in path for path in final_hashes)


@pytest.mark.skipif(agent_workspace.os.name != "nt", reason="仅验证 Windows 语义")
def test_windows_replace_uses_closed_same_directory_temporary_files(
    isolated_project,
):
    _project_root, code_root = isolated_project
    state = _workspace_with_changes(
        code_root, {"example.py": ("windows-old\n", "windows-new\n")}
    )

    agent_workspace.atomic_apply_workspace(state)

    assert (code_root / "example.py").read_text(encoding="utf-8") == "windows-new\n"
    assert not list(code_root.glob(".example.py.prepared-*"))
    assert not list(code_root.glob(".example.py.backup-*"))
