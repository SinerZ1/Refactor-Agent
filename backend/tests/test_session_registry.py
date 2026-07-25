import pytest

from session_registry import (
    ApprovalStateError,
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
    approval_id = registry.begin_approval(
        credentials.thread_id, credentials.session_token
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
    ) == (approval_id, True)
    assert (
        registry.consume_decision(credentials.thread_id, credentials.session_token)
        is None
    )


def test_pending_approval_id_is_idempotent_until_decided():
    registry = SessionRegistry()
    credentials = registry.create()

    first = registry.begin_approval(credentials.thread_id, credentials.session_token)
    second = registry.begin_approval(credentials.thread_id, credentials.session_token)

    assert first == second


def test_expired_session_is_removed():
    now = [100.0]
    registry = SessionRegistry(ttl_seconds=10, clock=lambda: now[0])
    credentials = registry.create()
    now[0] = 111.0

    with pytest.raises(SessionAuthorizationError):
        registry.require(credentials.thread_id, credentials.session_token)
