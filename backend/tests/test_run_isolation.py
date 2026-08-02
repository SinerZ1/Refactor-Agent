import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import agent
import app as app_module
from agent.events import make_agent_event
from session_registry import RunStateError, runtime_sessions


class _ConnectedRequest:
    async def is_disconnected(self):
        return False


def _collect_sse(response) -> list[dict]:
    async def collect() -> list[dict]:
        events = []
        async for chunk in response.body_iterator:
            text = chunk.decode() if isinstance(chunk, bytes) else chunk
            for frame in text.split("\n\n"):
                if frame.startswith("data: "):
                    events.append(json.loads(frame.removeprefix("data: ")))
        return events

    return asyncio.run(collect())


def test_stream_run_lease_rejects_overlap_and_scopes_every_event(monkeypatch):
    credentials = runtime_sessions.create()
    request = app_module.RefactorRequest(
        code="CodeSmells/main.py",
        thread_id=credentials.thread_id,
        session_token=credentials.session_token,
    )

    class FakeGraph:
        checkpointer = SimpleNamespace(delete_thread=lambda _thread_id: None)

        def get_state(self, _config):
            return SimpleNamespace(
                values={"review_status": "success"},
                interrupts=(),
            )

    def fake_stream(*_args, **_kwargs):
        yield make_agent_event("run.started", "开始")
        yield make_agent_event(
            "agent.message.delta",
            "完成",
            node="reviewer",
        )

    monkeypatch.setattr(agent, "app_graph", FakeGraph())
    monkeypatch.setattr(app_module, "stream_refactor", fake_stream)

    http_request = _ConnectedRequest()
    first_response = app_module.refactor_code_stream(request, http_request)
    with pytest.raises(HTTPException) as conflict:
        app_module.refactor_code_stream(request, http_request)
    assert conflict.value.status_code == 409

    first_events = _collect_sse(first_response)
    first_run_id = first_events[0]["run_id"]
    assert first_run_id.startswith("run_")
    assert all(event["run_id"] == first_run_id for event in first_events)
    assert first_events[-1]["type"] == "run.completed"

    with pytest.raises(RunStateError):
        runtime_sessions.require_active_run(
            credentials.thread_id, credentials.session_token
        )

    second_events = _collect_sse(app_module.refactor_code_stream(request, http_request))
    assert second_events[0]["run_id"] != first_run_id


def test_hitl_resume_keeps_run_identity_until_terminal_state(monkeypatch):
    credentials = runtime_sessions.create()
    request = app_module.RefactorRequest(
        code="CodeSmells/main.py",
        thread_id=credentials.thread_id,
        session_token=credentials.session_token,
    )
    lifecycle = {"resumed": False}
    workspace_id = "a" * 32
    workspace_digest = "b" * 64
    interrupt_payload = {
        "type": "aggregate_diff_approval",
        "file_path": "CodeSmells/main.py",
        "original_code": "old",
        "refactored_code": "new",
    }

    class FakeGraph:
        checkpointer = SimpleNamespace(delete_thread=lambda _thread_id: None)

        def get_state(self, _config):
            if lifecycle["resumed"]:
                return SimpleNamespace(
                    values={
                        "review_status": "success",
                        "workspace_id": workspace_id,
                        "workspace_applied": True,
                    },
                    interrupts=(),
                )
            return SimpleNamespace(
                values={
                    "review_status": "running",
                    "workspace_id": workspace_id,
                    "final_workspace_snapshot_digest": workspace_digest,
                    "review_evidence": {"workspace_snapshot_digest": workspace_digest},
                },
                interrupts=(SimpleNamespace(value=interrupt_payload),),
            )

    def fake_stream(_code, _thread_id, config, *_args, **_kwargs):
        if config["configurable"].get("resume_value"):
            lifecycle["resumed"] = True
        yield make_agent_event("run.started", "开始或恢复")

    monkeypatch.setattr(agent, "app_graph", FakeGraph())
    monkeypatch.setattr(app_module, "stream_refactor", fake_stream)
    monkeypatch.setattr(
        app_module,
        "build_workspace_snapshot",
        lambda _workspace_id: {"digest": workspace_digest},
    )
    monkeypatch.setattr(app_module, "cleanup_run_workspace", lambda _workspace_id: None)

    http_request = _ConnectedRequest()
    suspended_events = _collect_sse(
        app_module.refactor_code_stream(request, http_request)
    )
    waiting_event = next(
        event for event in suspended_events if event["type"] == "approval.waiting"
    )
    run_id = waiting_event["run_id"]
    assert (
        runtime_sessions.require_active_run(
            credentials.thread_id, credentials.session_token
        )
        == run_id
    )

    app_module.submit_approval(
        credentials.thread_id,
        app_module.ApprovalDecisionRequest(
            session_token=credentials.session_token,
            approval_id=waiting_event["payload"]["approval_id"],
            approved=True,
        ),
    )
    resumed_events = _collect_sse(
        app_module.refactor_code_stream(request, http_request)
    )

    assert lifecycle["resumed"] is True
    assert all(event["run_id"] == run_id for event in resumed_events)
    assert resumed_events[-1]["type"] == "run.completed"
    with pytest.raises(RunStateError):
        runtime_sessions.require_active_run(
            credentials.thread_id, credentials.session_token
        )
