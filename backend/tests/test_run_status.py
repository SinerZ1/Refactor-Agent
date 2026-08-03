from agent.run_status import RunStatusRegistry, run_statuses
from agent.workspace import public_workspace_apply_failure
from app import get_run_status_snapshot
from session_registry import runtime_sessions


def test_run_status_registry_enforces_ttl_capacity_and_run_isolation():
    now = [0.0]
    registry = RunStatusRegistry(ttl_seconds=10, capacity=2, clock=lambda: now[0])
    registry.start("run-a", "thread-a")
    now[0] = 1
    registry.start("run-b", "thread-b")
    now[0] = 2
    registry.start("run-c", "thread-c")

    assert registry.get("run-a", "thread-a") is None
    assert registry.get("run-b", "thread-a") is None
    assert registry.get("run-b", "thread-b") is not None
    assert len(registry) == 2

    now[0] = 20
    assert registry.get("run-b", "thread-b") is None
    assert len(registry) == 0


def test_run_status_snapshot_only_keeps_sanitized_fields():
    registry = RunStatusRegistry()
    registry.start("run-safe", "thread-safe")
    snapshot = registry.update(
        "run-safe",
        lifecycle_status="cleanup_failed",
        terminal_status="failed",
        cleanup_errors=["workspace:PermissionError"],
        apply_failure={
            "code": "atomic_apply_failed",
            "rollback_status": "partial",
            "affected_files": ["CodeSmells/example.py"],
        },
    )

    assert snapshot is not None
    serialized = str(snapshot)
    assert "Prompt" not in serialized
    assert "source_code" not in serialized
    assert "api_key" not in serialized
    assert set(snapshot) == {
        "version",
        "run_id",
        "lifecycle_status",
        "terminal_status",
        "termination_reason",
        "workspace_retained",
        "cleanup_errors",
        "apply_failure",
    }


def test_partial_rollback_public_payload_is_actionable_and_path_safe():
    payload = public_workspace_apply_failure(
        {
            "code": "atomic_apply_failed",
            "phase": "rollback",
            "conflicts": [
                "CodeSmells/example.py",
                "C:/Users/private/secret.py",
            ],
            "rollback_status": "partial",
            "recovery_artifacts": [
                "C:/repo/CodeSmells/.example.py.refactor-recovery-secret"
            ],
        },
        ["CodeSmells/example.py"],
    )

    assert payload is not None
    assert payload["rollback_status"] == "partial"
    assert payload["requires_manual_action"] is True
    assert payload["affected_files"] == ["CodeSmells/example.py"]
    assert payload["recovery_available"] is True
    assert payload["recovery_ids"]
    assert "C:/Users" not in str(payload)
    assert "secret.py" not in str(payload)


def test_complete_and_partial_rollback_have_distinct_public_contracts():
    complete = public_workspace_apply_failure(
        {
            "code": "atomic_apply_failed",
            "phase": "commit",
            "conflicts": [],
            "rollback_status": "complete",
            "recovery_artifacts": [],
        },
        ["CodeSmells/example.py"],
    )
    partial = public_workspace_apply_failure(
        {
            "code": "atomic_apply_failed",
            "phase": "rollback",
            "conflicts": ["CodeSmells/example.py"],
            "rollback_status": "partial",
            "recovery_artifacts": [],
        },
        ["CodeSmells/example.py"],
    )

    assert complete is not None and partial is not None
    assert complete["requires_manual_action"] is False
    assert partial["requires_manual_action"] is True
    assert complete["guidance"] != partial["guidance"]


def test_authenticated_snapshot_can_recover_terminal_state_after_sse_disconnect():
    credentials = runtime_sessions.create()
    run_statuses.start("run-snapshot-test", credentials.thread_id)
    run_statuses.update(
        "run-snapshot-test",
        lifecycle_status="cleanup_completed",
        terminal_status="failed",
        termination_reason="disconnect",
    )

    snapshot = get_run_status_snapshot(
        credentials.thread_id,
        "run-snapshot-test",
        session_token=credentials.session_token,
    )
    assert snapshot["lifecycle_status"] == "cleanup_completed"
    assert snapshot["terminal_status"] == "failed"
