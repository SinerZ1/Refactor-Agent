import threading
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode
from langgraph.types import Command

import agent.tools as agent_tools
import agent.workspace as agent_workspace
from agent.state import State, compute_change_set_digest


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
    changes: dict[str, tuple[str | None, str | None]],
) -> dict:
    for relative, (before, _after) in changes.items():
        if before is None:
            continue
        target = code_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(before, encoding="utf-8")
    state = agent_workspace.create_run_workspace()
    for relative, (_before, after) in changes.items():
        target = agent_workspace.resolve_workspace_path(
            state["workspace_id"], f"CodeSmells/{relative}"
        )
        if after is None:
            target.unlink()
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(after, encoding="utf-8")
    snapshot = agent_workspace.build_workspace_snapshot(state["workspace_id"])
    changed_files = agent_workspace.workspace_changed_paths(state["workspace_id"])
    change_records = [
        {
            "file_path": changed_files[0],
            "before_sha256": "before",
            "after_sha256": "after",
            "added_lines": 1,
            "removed_lines": 1,
            "unified_diff": "-before\n+after",
            "diff_truncated": False,
        }
    ]
    change_digest = compute_change_set_digest(change_records)
    state.update(
        change_records=change_records,
        test_run_records=[
            {
                "suite": "backend/behavior_tests (CodeSmells contract)",
                "success": True,
                "exit_code": 0,
                "change_set_digest": change_digest,
                "workspace_snapshot_digest": snapshot["digest"],
                "workspace_stable": True,
                "behavior_contract_included": True,
                "success_marker_present": True,
                "output_excerpt": agent_tools.TEST_SUCCESS_MARKER,
            }
        ],
        review_evidence={
            "changed_files": changed_files,
            "change_set_digest": change_digest,
            "workspace_snapshot_digest": snapshot["digest"],
            "test_suite": "backend/behavior_tests (CodeSmells contract)",
            "test_success": True,
            "test_exit_code": 0,
        },
    )
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


def test_unrelated_source_change_fails_full_baseline_check_before_writes(
    isolated_project,
):
    _project_root, code_root = isolated_project
    (code_root / "unrelated.py").write_text("original\n", encoding="utf-8")
    state = _workspace_with_changes(
        code_root, {"example.py": ("baseline\n", "candidate\n")}
    )
    (code_root / "unrelated.py").write_text("external\n", encoding="utf-8")

    with pytest.raises(agent_workspace.BaselineConflictError) as failure:
        agent_workspace.atomic_apply_workspace(state)

    assert failure.value.detail["phase"] == "全量预检"
    assert (code_root / "example.py").read_text(encoding="utf-8") == "baseline\n"
    assert (code_root / "unrelated.py").read_text(encoding="utf-8") == "external\n"


def test_external_change_after_preflight_before_first_replace_is_rejected(
    isolated_project,
):
    _project_root, code_root = isolated_project
    state = _workspace_with_changes(
        code_root, {"example.py": ("baseline\n", "candidate\n")}
    )

    def mutate_after_preflight(phase, _relative):
        if phase == "after_preflight":
            (code_root / "example.py").write_text("external\n", encoding="utf-8")

    with pytest.raises(agent_workspace.AtomicApplyError) as failure:
        agent_workspace.atomic_apply_workspace(state, hook=mutate_after_preflight)

    assert failure.value.detail["rollback_status"] == "not_required"
    assert failure.value.detail["conflicts"] == ["CodeSmells/example.py"]
    assert (code_root / "example.py").read_text(encoding="utf-8") == "external\n"
    assert not list(code_root.rglob("*.prepared-*"))
    assert not list(code_root.rglob("*.backup-*"))


def test_conflict_between_replacements_rolls_back_prior_file(isolated_project):
    _project_root, code_root = isolated_project
    state = _workspace_with_changes(
        code_root,
        {
            "first.py": ("first-old\n", "first-new\n"),
            "second.py": ("second-old\n", "second-new\n"),
        },
    )

    def mutate_second_after_first(phase, relative):
        if phase == "after_replace" and relative == "CodeSmells/first.py":
            (code_root / "second.py").write_text("external-second\n", encoding="utf-8")

    with pytest.raises(agent_workspace.AtomicApplyError) as failure:
        agent_workspace.atomic_apply_workspace(state, hook=mutate_second_after_first)

    assert failure.value.rolled_back is True
    assert failure.value.detail["rollback_status"] == "complete"
    assert (code_root / "first.py").read_text(encoding="utf-8") == "first-old\n"
    assert (code_root / "second.py").read_text(encoding="utf-8") == "external-second\n"


def test_rollback_never_overwrites_third_party_edit_and_preserves_backup(
    isolated_project,
):
    _project_root, code_root = isolated_project
    state = _workspace_with_changes(
        code_root,
        {
            "first.py": ("first-old\n", "first-new\n"),
            "second.py": ("second-old\n", "second-new\n"),
        },
    )

    def create_two_conflicts(phase, relative):
        if phase == "after_replace" and relative == "CodeSmells/first.py":
            (code_root / "first.py").write_text("third-party-first\n", encoding="utf-8")
            (code_root / "second.py").write_text(
                "third-party-second\n", encoding="utf-8"
            )

    with pytest.raises(agent_workspace.AtomicApplyError) as failure:
        agent_workspace.atomic_apply_workspace(state, hook=create_two_conflicts)

    assert failure.value.rolled_back is False
    assert failure.value.detail["rollback_status"] == "partial"
    assert failure.value.detail["conflicts"] == [
        "CodeSmells/second.py",
        "CodeSmells/first.py",
    ]
    assert failure.value.detail["recovery_artifacts"]
    assert (code_root / "first.py").read_text(encoding="utf-8") == "third-party-first\n"
    assert (code_root / "second.py").read_text(
        encoding="utf-8"
    ) == "third-party-second\n"
    recovery = list(code_root.glob(".first.py.refactor-recovery-*"))
    assert len(recovery) == 1
    assert recovery[0].read_text(encoding="utf-8") == "first-old\n"


def test_apply_node_keeps_frontend_error_and_adds_structured_partial_rollback(
    isolated_project, monkeypatch
):
    _project_root, code_root = isolated_project
    state = _workspace_with_changes(
        code_root, {"example.py": ("baseline\n", "candidate\n")}
    )
    graph_builder = StateGraph(State)
    graph_builder.add_node("apply", agent_workspace.apply_workspace_changes_node)
    graph_builder.add_edge(START, "apply")
    graph_builder.add_edge("apply", END)
    graph = graph_builder.compile(checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": "structured-partial-rollback"}}
    graph.invoke(state, config)

    failure = agent_workspace.AtomicApplyError(
        "需要人工处理",
        rolled_back=False,
        rollback_status="partial",
        conflicts=["CodeSmells/example.py"],
        recovery_artifacts=["CodeSmells/.example.py.refactor-recovery-test"],
    )
    monkeypatch.setattr(
        agent_workspace,
        "atomic_apply_workspace",
        lambda _state: (_ for _ in ()).throw(failure),
    )

    result = graph.invoke(Command(resume={"approved": True}), config)

    assert result["workspace_error"] == "需要人工处理"
    assert result["workspace_apply_failure"] == failure.detail
    assert result["workspace_apply_failure"]["rollback_status"] == "partial"


def test_new_file_is_removed_by_safe_compensation(isolated_project):
    _project_root, code_root = isolated_project
    state = _workspace_with_changes(
        code_root,
        {
            "new.py": (None, "created\n"),
            "second.py": ("second-old\n", "second-new\n"),
        },
    )

    def conflict_after_create(phase, relative):
        if phase == "after_replace" and relative == "CodeSmells/new.py":
            (code_root / "second.py").write_text("external\n", encoding="utf-8")

    with pytest.raises(agent_workspace.AtomicApplyError) as failure:
        agent_workspace.atomic_apply_workspace(state, hook=conflict_after_create)

    assert failure.value.detail["rollback_status"] == "complete"
    assert not (code_root / "new.py").exists()
    assert (code_root / "second.py").read_text(encoding="utf-8") == "external\n"


def test_deleted_file_is_restored_by_safe_compensation(isolated_project):
    _project_root, code_root = isolated_project
    state = _workspace_with_changes(
        code_root,
        {
            "first.py": ("first-old\n", None),
            "second.py": ("second-old\n", "second-new\n"),
        },
    )

    def conflict_after_delete(phase, relative):
        if phase == "after_replace" and relative == "CodeSmells/first.py":
            (code_root / "second.py").write_text("external\n", encoding="utf-8")

    with pytest.raises(agent_workspace.AtomicApplyError) as failure:
        agent_workspace.atomic_apply_workspace(state, hook=conflict_after_delete)

    assert failure.value.detail["rollback_status"] == "complete"
    assert (code_root / "first.py").read_text(encoding="utf-8") == "first-old\n"
    assert (code_root / "second.py").read_text(encoding="utf-8") == "external\n"


def test_target_replaced_by_directory_before_commit_fails_safely(isolated_project):
    _project_root, code_root = isolated_project
    state = _workspace_with_changes(
        code_root, {"example.py": ("baseline\n", "candidate\n")}
    )

    def replace_with_directory(phase, relative):
        if phase == "before_replace" and relative == "CodeSmells/example.py":
            (code_root / "example.py").unlink()
            (code_root / "example.py").mkdir()

    with pytest.raises(agent_workspace.AtomicApplyError) as failure:
        agent_workspace.atomic_apply_workspace(state, hook=replace_with_directory)

    assert failure.value.detail["rollback_status"] == "not_required"
    assert (code_root / "example.py").is_dir()
    assert not list(code_root.rglob("*.prepared-*"))
    assert not list(code_root.rglob("*.backup-*"))


def test_new_target_created_after_preflight_is_not_overwritten(isolated_project):
    _project_root, code_root = isolated_project
    state = _workspace_with_changes(code_root, {"new.py": (None, "candidate\n")})

    def create_external_file(phase, _relative):
        if phase == "after_preflight":
            (code_root / "new.py").write_text("external\n", encoding="utf-8")

    with pytest.raises(agent_workspace.AtomicApplyError):
        agent_workspace.atomic_apply_workspace(state, hook=create_external_file)

    assert (code_root / "new.py").read_text(encoding="utf-8") == "external\n"


def test_existing_target_deleted_after_preflight_is_not_recreated(isolated_project):
    _project_root, code_root = isolated_project
    state = _workspace_with_changes(
        code_root, {"example.py": ("baseline\n", "candidate\n")}
    )

    def delete_external_file(phase, _relative):
        if phase == "after_preflight":
            (code_root / "example.py").unlink()

    with pytest.raises(agent_workspace.AtomicApplyError):
        agent_workspace.atomic_apply_workspace(state, hook=delete_external_file)

    assert not (code_root / "example.py").exists()


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
    trusted_tests = agent_tools.PROJECT_ROOT / "backend" / "behavior_tests"
    assert str(trusted_tests) in captured["command"]
    assert str(working_root / "CodeSmells") not in captured["command"]
    assert captured["env"]["REFACTOR_CODE_ROOT"] == str(working_root / "CodeSmells")
    assert captured["env"]["PYTHONPATH"].split(agent_tools.os.pathsep)[0] == str(
        working_root
    )
    assert captured["env"]["PYTHONDONTWRITEBYTECODE"] == "1"


def test_reviewer_runs_trusted_contract_against_working_source():
    workspace_state = agent_workspace.create_run_workspace()
    graph_builder = StateGraph(State)
    graph_builder.add_node("tools", ToolNode([agent_tools.run_unit_tests]))
    graph_builder.add_edge(START, "tools")
    graph_builder.add_edge("tools", END)
    graph = graph_builder.compile()

    def invoke(call_id):
        return graph.invoke(
            {
                "messages": [
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "run_unit_tests",
                                "args": {"test_suite": "codesmells"},
                                "id": call_id,
                                "type": "tool_call",
                            }
                        ],
                    )
                ],
                "retry_count": 0,
                "change_records": [],
                "test_run_records": [],
                **workspace_state,
            }
        )

    try:
        passing = invoke("trusted-pass")
        assert passing["test_run_records"][-1]["success"] is True

        candidate = agent_workspace.resolve_workspace_path(
            workspace_state["workspace_id"], "CodeSmells/Calculator.py"
        )
        candidate.write_text(
            "def calc(_a, _b, _operator):\n    return -999\n",
            encoding="utf-8",
        )
        failing = invoke("trusted-fail")
        assert failing["test_run_records"][-1]["success"] is False
        assert failing["test_run_records"][-1]["exit_code"] != 0
    finally:
        agent_workspace.cleanup_run_workspace(workspace_state["workspace_id"])


def test_aggregate_and_apply_reject_protected_test_changes(isolated_project):
    _project_root, code_root = isolated_project
    protected = code_root / "tests" / "test_contract.py"
    protected.parent.mkdir()
    protected.write_text("def test_contract():\n    assert True\n", encoding="utf-8")
    state = agent_workspace.create_run_workspace()
    working_protected = (
        agent_workspace.WORKSPACE_ROOT
        / state["workspace_id"]
        / "working"
        / "CodeSmells"
        / "tests"
        / "test_contract.py"
    )
    working_protected.write_text(
        "def test_contract():\n    assert False\n", encoding="utf-8"
    )

    with pytest.raises(agent_workspace.WorkspaceError, match="行为契约测试"):
        agent_workspace.build_aggregate_diff(state["workspace_id"])

    working_root = agent_workspace.WORKSPACE_ROOT / state["workspace_id"] / "working"
    state["final_file_hashes"] = agent_workspace._hash_snapshot(working_root)
    with pytest.raises(agent_workspace.WorkspaceError, match="行为契约测试"):
        agent_workspace.atomic_apply_workspace(state)
    assert "assert True" in protected.read_text(encoding="utf-8")


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


def test_same_source_root_apply_commit_sections_never_overlap(isolated_project):
    _project_root, code_root = isolated_project
    first_state = _workspace_with_changes(
        code_root, {"example.py": ("baseline\n", "first\n")}
    )
    second_state = _workspace_with_changes(
        code_root, {"example.py": ("baseline\n", "second\n")}
    )
    first_entered = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()
    second_entered = threading.Event()
    failures = []

    def first_hook(phase, _relative):
        if phase == "before_commit":
            first_entered.set()
            release_first.wait()

    def second_hook(phase, _relative):
        if phase == "before_commit":
            second_entered.set()

    def apply_first():
        agent_workspace.atomic_apply_workspace(first_state, hook=first_hook)

    def apply_second():
        second_started.set()
        try:
            agent_workspace.atomic_apply_workspace(second_state, hook=second_hook)
        except Exception as exc:
            failures.append(exc)

    first_thread = threading.Thread(target=apply_first)
    second_thread = threading.Thread(target=apply_second)
    first_thread.start()
    assert first_entered.wait(timeout=1)
    second_thread.start()
    assert second_started.wait(timeout=1)
    assert not second_entered.is_set()

    release_first.set()
    first_thread.join(timeout=1)
    second_thread.join(timeout=1)

    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert len(failures) == 1
    assert isinstance(failures[0], agent_workspace.BaselineConflictError)
    assert (code_root / "example.py").read_text(encoding="utf-8") == "first\n"
    assert not agent_workspace._APPLY_LOCKS


def test_different_source_root_locks_can_progress_concurrently(tmp_path):
    first_root = tmp_path / "first" / "CodeSmells"
    second_root = tmp_path / "second" / "CodeSmells"
    first_root.mkdir(parents=True)
    second_root.mkdir(parents=True)
    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()

    def hold_first_root():
        with agent_workspace._apply_critical_section(first_root):
            first_entered.set()
            release_first.wait()

    def enter_second_root():
        with agent_workspace._apply_critical_section(second_root):
            second_entered.set()

    first_thread = threading.Thread(target=hold_first_root)
    second_thread = threading.Thread(target=enter_second_root)
    first_thread.start()
    assert first_entered.wait(timeout=1)
    second_thread.start()

    assert second_entered.wait(timeout=1)
    release_first.set()
    first_thread.join(timeout=1)
    second_thread.join(timeout=1)
    assert not agent_workspace._APPLY_LOCKS


def test_reparse_point_target_is_rejected_before_any_write(
    isolated_project, monkeypatch
):
    _project_root, code_root = isolated_project
    state = _workspace_with_changes(
        code_root, {"example.py": ("baseline\n", "candidate\n")}
    )
    target = code_root / "example.py"
    monkeypatch.setattr(
        agent_workspace,
        "_is_reparse_point",
        lambda path: path == target,
    )

    with pytest.raises(agent_workspace.WorkspaceError, match="reparse point"):
        agent_workspace.atomic_apply_workspace(state)

    assert target.read_text(encoding="utf-8") == "baseline\n"
