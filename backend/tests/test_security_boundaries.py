from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from langchain_core.messages import AIMessage
from langgraph.checkpoint.base import get_checkpoint_metadata
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode
from pydantic import ValidationError

import agent.tools as agent_tools
from agent.credentials import (
    CredentialReferenceError,
    EphemeralCredentialVault,
    runtime_credentials,
)
from agent.state import State
from app import RefactorRequest, authorize_refactor_request, build_graph_config
from session_registry import runtime_sessions


def _invoke_test_tool(test_suite: str):
    tool_call = {
        "name": "run_unit_tests",
        "args": {"test_suite": test_suite},
        "id": "security-test-call",
        "type": "tool_call",
    }
    graph_builder = StateGraph(State)
    graph_builder.add_node("tools", ToolNode([agent_tools.run_unit_tests]))
    graph_builder.add_edge(START, "tools")
    graph_builder.add_edge("tools", END)
    graph = graph_builder.compile()
    return graph.invoke(
        {
            "messages": [AIMessage(content="", tool_calls=[tool_call])],
            "retry_count": 0,
            "change_records": [],
            "test_run_records": [],
        }
    )


def test_graph_config_replaces_api_key_with_opaque_reference():
    request = RefactorRequest.model_validate(
        {
            "code": "CodeSmells/main.py",
            "thread_id": "security-test",
            "session_token": "session-test-secret",
            "model_config": {
                "provider": "openai",
                "api_key": "unit-test-secret",
                "base_url": "https://1.1.1.1",
                "model_name": "test-model",
            },
        }
    )

    config, credential_ref = build_graph_config(request)
    try:
        configurable = config["configurable"]
        metadata = get_checkpoint_metadata(config, {})

        assert "api_key" not in configurable
        assert "session_token" not in configurable
        assert "unit-test-secret" not in repr(config)
        assert "session-test-secret" not in repr(config)
        assert "unit-test-secret" not in repr(metadata)
        assert configurable["credential_ref"] == credential_ref
        assert configurable["run_budget"]["max_agent_steps"] == 24
        assert config["recursion_limit"] > 24
        assert runtime_credentials.resolve(credential_ref or "") == "unit-test-secret"
    finally:
        runtime_credentials.revoke(credential_ref)


def test_run_budget_rejects_attempts_to_disable_safety_limits():
    with pytest.raises(ValidationError):
        RefactorRequest.model_validate(
            {
                "code": "CodeSmells/main.py",
                "run_budget": {
                    "max_agent_steps": 0,
                    "max_tool_calls": 0,
                    "max_total_tokens": 0,
                    "model_timeout_seconds": 0,
                },
            }
        )


def test_custom_run_budget_enters_graph_config_and_recursion_guard():
    request = RefactorRequest.model_validate(
        {
            "code": "CodeSmells/main.py",
            "run_budget": {
                "max_agent_steps": 6,
                "max_tool_calls": 7,
                "max_total_tokens": 8000,
                "model_timeout_seconds": 15,
            },
        }
    )

    config, credential_ref = build_graph_config(request)

    assert credential_ref is None
    assert config["configurable"]["run_budget"]["model_timeout_seconds"] == 15
    assert config["recursion_limit"] == 42


def test_runtime_credential_reference_is_revoked():
    vault = EphemeralCredentialVault(ttl_seconds=60)
    credential_ref = vault.store("unit-test-secret")

    assert credential_ref
    assert vault.resolve(credential_ref) == "unit-test-secret"
    vault.revoke(credential_ref)

    with pytest.raises(CredentialReferenceError):
        vault.resolve(credential_ref)


def test_request_cannot_override_process_adc_path():
    with pytest.raises(ValidationError):
        RefactorRequest.model_validate(
            {
                "code": "CodeSmells/main.py",
                "model_config": {
                    "provider": "google_vertex",
                    "vertex_adc_path": "C:/sensitive/credentials.json",
                },
            }
        )


def test_refactor_request_requires_backend_issued_session():
    credentials = runtime_sessions.create()
    authorized_request = RefactorRequest(
        code="CodeSmells/main.py",
        thread_id=credentials.thread_id,
        session_token=credentials.session_token,
    )
    assert authorize_refactor_request(authorized_request) == credentials.session_token

    unauthorized_request = RefactorRequest(
        code="CodeSmells/main.py",
        thread_id=credentials.thread_id,
        session_token="wrong-token",
    )
    with pytest.raises(HTTPException) as exc_info:
        authorize_refactor_request(unauthorized_request)
    assert exc_info.value.status_code == 401


@pytest.mark.parametrize(
    "unsafe_path",
    [
        "../backend/.env",
        "CodeSmells/../../backend/.env",
        "C:/Users/example/.ssh/id_rsa",
    ],
)
def test_resolve_path_rejects_workspace_escape(unsafe_path: str):
    with pytest.raises(ValueError):
        agent_tools.resolve_path(unsafe_path)


def test_resolve_path_accepts_codesmells_relative_path():
    resolved = agent_tools.resolve_path("CodeSmells/main.py")

    assert resolved.endswith("CodeSmells\\main.py")


def test_unit_test_tool_uses_fixed_argv_without_shell(monkeypatch):
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return SimpleNamespace(stdout="tests passed", stderr="", returncode=0)

    monkeypatch.setattr(agent_tools.subprocess, "run", fake_run)

    result = _invoke_test_tool("backend")

    assert captured["shell"] is False
    assert captured["command"][-1] == "backend/tests"
    assert captured["command"][1:5] == [
        "-m",
        "pytest",
        "-p",
        "no:cacheprovider",
    ]
    assert "tests passed" in result["messages"][-1].content


def test_unit_test_tool_rejects_command_injection():
    with pytest.raises(ValidationError):
        agent_tools.run_unit_tests.get_input_schema().model_validate(
            {"test_suite": "backend; whoami"}
        )
