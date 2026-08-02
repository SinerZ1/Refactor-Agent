import pytest

from session_registry import (
    ApprovalStateError,
    RunStateError,
    SessionAuthorizationError,
    SessionRegistry,
)


def test_session_requires_unpredictable_token():
    registry = SessionRegistry()
    credentials = registry.create()

    registry.require(credentials.thread_id, credentials.session_token)
    with pytest.raises(SessionAuthorizationError):
        registry.require(credentials.thread_id, "wrong-token")


def test_approval_decision_is_bound_to_current_nonce():
    registry = SessionRegistry()
    credentials = registry.create()
    run_id = registry.begin_run(credentials.thread_id, credentials.session_token)
    approval_id = registry.begin_approval(
        credentials.thread_id, credentials.session_token, run_id
    )

    with pytest.raises(ApprovalStateError):
        registry.decide(
            credentials.thread_id,
            credentials.session_token,
            "stale-approval",
            True,
        )

    registry.decide(
        credentials.thread_id,
        credentials.session_token,
        approval_id,
        True,
    )
    assert registry.consume_decision(
        credentials.thread_id, credentials.session_token
    ) == (approval_id, True, run_id)
    assert (
        registry.consume_decision(credentials.thread_id, credentials.session_token)
        is None
    )


def test_pending_approval_id_is_idempotent_until_decided():
    registry = SessionRegistry()
    credentials = registry.create()
    run_id = registry.begin_run(credentials.thread_id, credentials.session_token)

    first = registry.begin_approval(
        credentials.thread_id, credentials.session_token, run_id
    )
    second = registry.begin_approval(
        credentials.thread_id, credentials.session_token, run_id
    )

    assert first == second


def test_hitl_retention_requires_matching_active_run_and_pending_approval():
    registry = SessionRegistry()
    credentials = registry.create()
    run_id = registry.begin_run(credentials.thread_id, credentials.session_token)
    approval_id = registry.begin_approval(
        credentials.thread_id, credentials.session_token, run_id
    )

    assert registry.is_resumable_approval(credentials.thread_id, run_id, approval_id)
    assert not registry.is_resumable_approval(
        credentials.thread_id, "stale-run", approval_id
    )
    registry.decide(
        credentials.thread_id,
        credentials.session_token,
        approval_id,
        False,
    )
    assert not registry.is_resumable_approval(
        credentials.thread_id, run_id, approval_id
    )


def test_internal_lease_release_only_accepts_current_run_id():
    registry = SessionRegistry()
    credentials = registry.create()
    run_id = registry.begin_run(credentials.thread_id, credentials.session_token)

    assert not registry.finish_owned_run(credentials.thread_id, "stale-run")
    assert registry.finish_owned_run(credentials.thread_id, run_id)
    assert not registry.finish_owned_run(credentials.thread_id, run_id)


def test_single_session_run_lease_rejects_concurrent_dag_execution():
    registry = SessionRegistry()
    credentials = registry.create()

    first_run = registry.begin_run(credentials.thread_id, credentials.session_token)
    with pytest.raises(RunStateError):
        registry.begin_run(credentials.thread_id, credentials.session_token)

    assert (
        registry.finish_run(
            credentials.thread_id, credentials.session_token, "run_stale"
        )
        is False
    )
    assert (
        registry.require_active_run(credentials.thread_id, credentials.session_token)
        == first_run
    )
    assert registry.finish_run(
        credentials.thread_id, credentials.session_token, first_run
    )

    second_run = registry.begin_run(credentials.thread_id, credentials.session_token)
    assert second_run != first_run


def test_expired_session_is_removed():
    now = [100.0]
    registry = SessionRegistry(ttl_seconds=10, clock=lambda: now[0])
    credentials = registry.create()
    now[0] = 111.0

    with pytest.raises(SessionAuthorizationError):
        registry.require(credentials.thread_id, credentials.session_token)
