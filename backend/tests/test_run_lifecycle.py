import asyncio
import threading
from collections import Counter
from pathlib import Path

import pytest

from agent.run_lifecycle import (
    ActiveRunRegistry,
    HitlRetention,
    RunLifecycle,
    RunTermination,
)
from agent.run_status import run_statuses


def _lifecycle(
    tmp_path: Path,
    *,
    run_id: str = "run-test",
    registry: ActiveRunRegistry | None = None,
    timeout: float = 0.2,
    cleanup_workspace_override=None,
):
    calls: Counter[str] = Counter()
    workspace = tmp_path / run_id
    workspace.mkdir()
    owner = registry or ActiveRunRegistry()

    def counted(name, result=None):
        def operation(*_args):
            calls[name] += 1
            return result

        return operation

    def cleanup_workspace(_workspace_id):
        calls["workspace"] += 1
        workspace.rmdir()

    lifecycle = RunLifecycle(
        run_id=run_id,
        thread_id=f"thread-{run_id}",
        credential_ref="opaque-reference",
        finish_lease=counted("lease", True),
        revoke_credential=counted("credential"),
        delete_checkpoint=counted("checkpoint"),
        cleanup_workspace=cleanup_workspace_override or cleanup_workspace,
        resolve_workspace=lambda: run_id,
        unregister=owner.unregister,
        producer_timeout_seconds=timeout,
    )
    lifecycle.set_workspace(run_id)
    owner.register(lifecycle)
    return lifecycle, owner, calls, workspace


def test_disconnect_stops_and_joins_producer_before_workspace_cleanup(tmp_path):
    async def scenario():
        lifecycle, _registry, calls, workspace = _lifecycle(tmp_path)
        producer_stopped = threading.Event()

        def producer():
            lifecycle.stop_requested.wait()
            producer_stopped.set()

        lifecycle.start_producer(producer)
        report = await lifecycle.cleanup(RunTermination.DISCONNECTED)

        assert producer_stopped.is_set()
        assert lifecycle.producer_alive is False
        assert report.workspace_cleaned is True
        assert not workspace.exists()
        assert calls == Counter(
            {"credential": 1, "checkpoint": 1, "lease": 1, "workspace": 1}
        )

    asyncio.run(scenario())


def test_cleanup_is_idempotent_and_resources_release_once(tmp_path, capsys):
    async def scenario():
        lifecycle, _registry, calls, _workspace = _lifecycle(tmp_path)
        lifecycle.start_producer(lambda: None)

        first, second = await asyncio.gather(
            lifecycle.cleanup(RunTermination.FAILED),
            lifecycle.cleanup(RunTermination.FAILED),
        )

        assert first is second
        assert calls == Counter(
            {"credential": 1, "checkpoint": 1, "lease": 1, "workspace": 1}
        )

    asyncio.run(scenario())
    assert "duplicate_cleanup_ignored" in capsys.readouterr().out


def test_producer_exception_is_retrieved_without_secret_leak(tmp_path, capsys):
    async def scenario():
        lifecycle, _registry, _calls, _workspace = _lifecycle(tmp_path)

        def producer():
            raise RuntimeError("api-key=super-secret")

        lifecycle.start_producer(producer)
        report = await lifecycle.cleanup(RunTermination.FAILED)
        assert report.producer_error == "RuntimeError"

    asyncio.run(scenario())
    output = capsys.readouterr().out
    assert "producer_exception_retrieved" in output
    assert "super-secret" not in output


def test_cancellation_waits_for_critical_cleanup_then_propagates(tmp_path):
    entered = threading.Event()
    release = threading.Event()

    def blocking_workspace_cleanup(workspace_id):
        entered.set()
        release.wait()
        (tmp_path / workspace_id).rmdir()

    async def scenario():
        lifecycle, _registry, calls, workspace = _lifecycle(
            tmp_path,
            cleanup_workspace_override=blocking_workspace_cleanup,
        )
        lifecycle.start_producer(lambda: None)
        caller = asyncio.create_task(lifecycle.cleanup(RunTermination.CANCELLATION))
        await asyncio.to_thread(entered.wait)
        caller.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await caller

        assert not workspace.exists()
        assert calls["credential"] == 1
        assert calls["lease"] == 1

    asyncio.run(scenario())


def test_unresponsive_producer_uses_bounded_deferred_cleanup(tmp_path):
    release = threading.Event()

    async def scenario():
        lifecycle, _registry, calls, workspace = _lifecycle(
            tmp_path,
            timeout=0.01,
        )
        lifecycle.start_producer(release.wait)
        report = await lifecycle.cleanup(RunTermination.DISCONNECTED)

        assert report.producer_timed_out is True
        pending = run_statuses.get("run-test", "thread-run-test")
        assert pending is not None
        assert pending["lifecycle_status"] == "cleanup_pending"
        assert workspace.exists()
        assert not calls

        release.set()
        assert lifecycle._deferred_cleanup_task is not None
        await lifecycle._deferred_cleanup_task
        completed = run_statuses.get("run-test", "thread-run-test")
        assert completed is not None
        assert completed["lifecycle_status"] == "cleanup_completed"
        assert not workspace.exists()
        assert calls["workspace"] == 1

    asyncio.run(scenario())


def test_hitl_pause_retains_only_recoverable_workspace_and_shutdown_cleans_it(
    tmp_path,
):
    async def scenario():
        lifecycle, registry, calls, workspace = _lifecycle(tmp_path)
        lifecycle.start_producer(lambda: None)
        lifecycle.retain_for_hitl(
            HitlRetention(
                workspace_id="run-test",
                approval_id="approval-id",
                workspace_snapshot_digest="a" * 64,
            )
        )

        paused = await lifecycle.cleanup(
            RunTermination.HITL_PAUSED,
            retain_for_hitl=True,
        )
        assert paused.workspace_retained is True
        retained = run_statuses.get("run-test", "thread-run-test")
        assert retained is not None
        assert retained["lifecycle_status"] == "waiting_for_hitl"
        assert workspace.exists()
        assert calls == Counter({"credential": 1})

        reports = await registry.shutdown()
        assert len(reports) == 1
        assert not workspace.exists()
        assert calls["credential"] == 1
        assert calls["checkpoint"] == 1
        assert calls["lease"] == 1

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "termination",
    [
        RunTermination.COMPLETED,
        RunTermination.HITL_APPROVED,
        RunTermination.HITL_REJECTED,
        RunTermination.RECOVERY_FAILED,
        RunTermination.BUDGET_EXCEEDED,
    ],
)
def test_all_non_hitl_terminal_states_delete_workspace(tmp_path, termination):
    async def scenario():
        lifecycle, _registry, calls, workspace = _lifecycle(
            tmp_path,
            run_id=termination.value,
        )
        lifecycle.start_producer(lambda: None)
        report = await lifecycle.cleanup(termination)

        assert report.workspace_retained is False
        assert report.workspace_cleaned is True
        assert not workspace.exists()
        assert calls["checkpoint"] == 1

    asyncio.run(scenario())


def test_concurrent_run_cleanup_is_isolated(tmp_path):
    async def scenario():
        first, registry, first_calls, first_workspace = _lifecycle(
            tmp_path,
            run_id="first",
        )
        second, _registry, second_calls, second_workspace = _lifecycle(
            tmp_path,
            run_id="second",
            registry=registry,
        )
        first.start_producer(lambda: None)
        second.start_producer(lambda: None)

        await first.cleanup(RunTermination.DISCONNECTED)

        assert not first_workspace.exists()
        assert second_workspace.exists()
        assert registry.get("second") is second
        assert first_calls["workspace"] == 1
        assert not second_calls
        await second.cleanup(RunTermination.COMPLETED)

    asyncio.run(scenario())


def test_active_registry_shutdown_reclaims_multiple_runs(tmp_path):
    async def scenario():
        registry = ActiveRunRegistry()
        lifecycles = [
            _lifecycle(tmp_path, run_id=name, registry=registry)[0]
            for name in ("shutdown-a", "shutdown-b")
        ]
        for lifecycle in lifecycles:
            lifecycle.start_producer(
                lambda lifecycle=lifecycle: lifecycle.stop_requested.wait()
            )

        reports = await registry.shutdown()

        assert len(reports) == 2
        assert all(not lifecycle.producer_alive for lifecycle in lifecycles)
        assert registry.get("shutdown-a") is None
        assert registry.get("shutdown-b") is None

    asyncio.run(scenario())


def test_cleanup_failure_does_not_skip_remaining_resources(tmp_path):
    def fail_workspace(_workspace_id):
        raise PermissionError("sensitive-source-path")

    async def scenario():
        lifecycle, _registry, calls, workspace = _lifecycle(
            tmp_path,
            cleanup_workspace_override=fail_workspace,
        )
        lifecycle.start_producer(lambda: None)
        report = await lifecycle.cleanup(RunTermination.FAILED)

        assert report.cleanup_complete is False
        assert report.cleanup_errors == ["workspace:PermissionError"]
        assert workspace.exists()
        assert calls["credential"] == 1
        assert calls["checkpoint"] == 1
        assert calls["lease"] == 1
        failed = run_statuses.get("run-test", "thread-run-test")
        assert failed is not None
        assert failed["lifecycle_status"] == "cleanup_failed"

    asyncio.run(scenario())
