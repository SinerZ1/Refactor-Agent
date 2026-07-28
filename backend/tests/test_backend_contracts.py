import asyncio
import socket
import threading

import pytest
from fastapi import HTTPException
from langgraph.checkpoint.memory import MemorySaver
from pydantic import ValidationError

import agent
import app as app_module
import graph_indexer
import network_security
from agent.credentials import CredentialReferenceError, runtime_credentials
from agent.workflow import get_checkpointer
from network_security import ModelBaseUrlError, validate_model_base_url
from session_registry import RunStateError, runtime_sessions


def _dns_result(address: str, port: int = 443):
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    sockaddr = (address, port, 0, 0) if family == socket.AF_INET6 else (address, port)
    return (family, socket.SOCK_STREAM, 6, "", sockaddr)


def test_base_url_rejects_non_http_credentials_and_fragment():
    for value in (
        "file:///etc/passwd",
        "https://user:secret@example.com/v1",
        "https://example.com/v1#admin",
    ):
        with pytest.raises(ModelBaseUrlError):
            validate_model_base_url(
                value,
                resolver=lambda *_args, **_kwargs: [_dns_result("93.184.216.34")],
            )


def test_remote_bind_rejects_dns_alias_to_private_address(monkeypatch):
    monkeypatch.setenv("BACKEND_HOST", "0.0.0.0")
    monkeypatch.delenv("ALLOW_PRIVATE_MODEL_BASE_URL", raising=False)
    monkeypatch.setattr(
        network_security.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [_dns_result("192.168.1.20")],
    )

    with pytest.raises(ModelBaseUrlError, match="回环或私网"):
        validate_model_base_url("https://models.example.test/v1")


def test_remote_bind_rejects_mixed_public_and_private_dns_answers(monkeypatch):
    monkeypatch.setenv("BACKEND_HOST", "0.0.0.0")
    monkeypatch.delenv("ALLOW_PRIVATE_MODEL_BASE_URL", raising=False)
    monkeypatch.setattr(
        network_security.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            _dns_result("93.184.216.34"),
            _dns_result("127.0.0.1"),
        ],
    )

    with pytest.raises(ModelBaseUrlError, match="回环或私网"):
        validate_model_base_url("https://rebind.example.test/v1")


def test_loopback_bind_allows_explicit_local_model_but_never_metadata(monkeypatch):
    monkeypatch.setenv("BACKEND_HOST", "127.0.0.1")
    monkeypatch.delenv("ALLOW_PRIVATE_MODEL_BASE_URL", raising=False)
    monkeypatch.setattr(
        network_security.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [_dns_result("127.0.0.1", 11434)],
    )

    assert (
        validate_model_base_url("http://local-model.test:11434/v1")
        == "http://local-model.test:11434/v1"
    )

    monkeypatch.setattr(
        network_security.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [_dns_result("169.254.169.254")],
    )
    with pytest.raises(ModelBaseUrlError, match="元数据"):
        validate_model_base_url("http://metadata-alias.test/latest")


def test_request_validation_returns_clear_4xx_for_invalid_base_url():
    with pytest.raises(ValidationError, match="仅允许使用 http 或 https"):
        app_module.RefactorRequest.model_validate(
            {
                "code": "CodeSmells/main.py",
                "model_config": {
                    "provider": "openai",
                    "base_url": "ftp://models.example.test",
                },
            }
        )


def test_build_graph_config_rejects_private_dns_for_remote_deployment(monkeypatch):
    monkeypatch.setenv("BACKEND_HOST", "0.0.0.0")
    monkeypatch.delenv("ALLOW_PRIVATE_MODEL_BASE_URL", raising=False)
    monkeypatch.setattr(
        network_security.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [_dns_result("10.0.0.8")],
    )
    request = app_module.RefactorRequest.model_validate(
        {
            "code": "CodeSmells/main.py",
            "model_config": {
                "provider": "openai",
                "base_url": "https://internal-model.example.test/v1",
            },
        }
    )

    with pytest.raises(HTTPException) as exc_info:
        app_module.build_graph_config(request)
    assert exc_info.value.status_code == 422
    assert "回环或私网" in str(exc_info.value.detail)


def test_redis_failure_log_never_exposes_credentials_or_query(monkeypatch, capsys):
    secret = "redis-password-unit-test"
    monkeypatch.setenv(
        "REDIS_URL",
        f"redis://redis-user:{secret}@127.0.0.1:6379/0?token=query-secret",
    )

    def fail_connection(*_args, **_kwargs):
        raise ConnectionError(
            f"failed redis://redis-user:{secret}@127.0.0.1?token=query-secret"
        )

    monkeypatch.setattr("redis.Redis.from_url", fail_connection)

    assert isinstance(get_checkpointer(), MemorySaver)
    output = capsys.readouterr().out
    assert secret not in output
    assert "redis-user" not in output
    assert "query-secret" not in output
    assert "redis://127.0.0.1:6379/0" in output


def test_neo4j_missing_credentials_skips_driver_and_uses_ast(monkeypatch):
    monkeypatch.setenv("NEO4J_URI", "bolt://127.0.0.1:7687")
    monkeypatch.delenv("NEO4J_USER", raising=False)
    monkeypatch.delenv("NEO4J_PASSWORD", raising=False)

    def unexpected_driver(*_args, **_kwargs):
        raise AssertionError("未配置凭据时不应创建 Neo4j driver")

    monkeypatch.setattr(graph_indexer.GraphDatabase, "driver", unexpected_driver)

    assert graph_indexer.get_neo4j_driver() is None
    assert graph_indexer.get_neo4j_health()["status"] == "degraded"
    assert graph_indexer.get_neo4j_health()["backend"] == "ast"


def test_neo4j_connection_failure_closes_driver_and_redacts_error(monkeypatch, capsys):
    secret = "neo4j-password-unit-test"
    monkeypatch.setenv("NEO4J_USER", "neo4j-user")
    monkeypatch.setenv("NEO4J_PASSWORD", secret)
    state = {"closed": False}

    class FakeDriver:
        def verify_connectivity(self):
            raise RuntimeError(f"authentication failed with {secret}")

        def close(self):
            state["closed"] = True

    monkeypatch.setattr(
        graph_indexer.GraphDatabase,
        "driver",
        lambda *_args, **_kwargs: FakeDriver(),
    )

    assert graph_indexer.get_neo4j_driver() is None
    assert state["closed"] is True
    assert secret not in capsys.readouterr().out


def test_health_reports_optional_component_degradation_without_failing():
    health = app_module.health_check()

    assert health["status"] == "ok"
    assert health["components"]["service"]["status"] == "ok"
    assert health["components"]["redis"]["status"] in {"ok", "degraded"}
    assert health["components"]["neo4j"]["status"] in {"ok", "degraded"}


def test_openapi_only_exposes_hitl_capable_refactor_endpoint():
    paths = app_module.app.openapi()["paths"]

    assert "/api/refactor" not in paths
    assert "/api/refactor/stream" in paths


class _ConnectedRequest:
    async def is_disconnected(self):
        return False


class _DisconnectedRequest:
    async def is_disconnected(self):
        return True


def test_sse_headers_and_heartbeat_are_transport_only(monkeypatch):
    credentials = runtime_sessions.create()
    request = app_module.RefactorRequest(
        code="CodeSmells/main.py",
        thread_id=credentials.thread_id,
        session_token=credentials.session_token,
    )
    release_producer = threading.Event()

    def delayed_stream(*_args, **_kwargs):
        release_producer.wait(timeout=1)
        if False:
            yield

    monkeypatch.setattr(app_module, "SSE_HEARTBEAT_SECONDS", 0.01)
    monkeypatch.setattr(app_module, "stream_refactor", delayed_stream)
    response = app_module.refactor_code_stream(request, _ConnectedRequest())

    async def collect_heartbeat():
        iterator = response.body_iterator
        frame = await anext(iterator)
        await iterator.aclose()
        return frame

    try:
        frame = asyncio.run(collect_heartbeat())
    finally:
        release_producer.set()

    assert frame == ": heartbeat\n\n"
    assert not frame.startswith("data:")
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"
    with pytest.raises(RunStateError):
        runtime_sessions.require_active_run(
            credentials.thread_id,
            credentials.session_token,
        )


def test_sse_disconnect_releases_run_lease_and_ephemeral_credential(monkeypatch):
    credentials = runtime_sessions.create()
    request = app_module.RefactorRequest.model_validate(
        {
            "code": "CodeSmells/main.py",
            "thread_id": credentials.thread_id,
            "session_token": credentials.session_token,
            "model_config": {
                "provider": "openai",
                "api_key": "disconnect-secret",
                "base_url": "https://1.1.1.1",
            },
        }
    )
    captured_ref = {}
    original_store = runtime_credentials.store

    def capture_store(secret):
        credential_ref = original_store(secret)
        captured_ref["value"] = credential_ref
        return credential_ref

    monkeypatch.setattr(runtime_credentials, "store", capture_store)
    response = app_module.refactor_code_stream(request, _DisconnectedRequest())

    async def start_then_disconnect():
        iterator = response.body_iterator
        with pytest.raises(StopAsyncIteration):
            await anext(iterator)

    asyncio.run(start_then_disconnect())

    with pytest.raises(RunStateError):
        runtime_sessions.require_active_run(
            credentials.thread_id,
            credentials.session_token,
        )
    with pytest.raises(CredentialReferenceError):
        runtime_credentials.resolve(captured_ref["value"])


def test_simple_refactor_propagates_workflow_exception(monkeypatch):
    monkeypatch.setattr(
        agent.app_graph,
        "invoke",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    with pytest.raises(RuntimeError, match="boom"):
        agent.simple_refactor("CodeSmells/main.py")
