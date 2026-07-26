import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import agent
import app as app_module
from agent.events import make_agent_event
from session_registry import RunStateError, runtime_sessions


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

    first_response = app_module.refactor_code_stream(request)
    with pytest.raises(HTTPException) as conflict:
        app_module.refactor_code_stream(request)
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

    second_events = _collect_sse(app_module.refactor_code_stream(request))
    assert second_events[0]["run_id"] != first_run_id
