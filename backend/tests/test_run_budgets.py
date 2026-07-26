from langchain_core.messages import AIMessage, HumanMessage

from agent.budgets import (
    account_agent_response,
    budget_preflight_reason,
    empty_run_usage,
    extract_token_usage,
)
from agent.nodes import call_developer, get_llm_from_config


def _config(**overrides):
    return {
        "configurable": {
            "run_budget": {
                "max_agent_steps": 8,
                "max_tool_calls": 4,
                "max_total_tokens": 10_000,
                "model_timeout_seconds": 30,
                **overrides,
            }
        }
    }


def test_usage_accounting_normalizes_provider_metadata():
    response = AIMessage(
        content="完成",
        usage_metadata={
            "input_tokens": 120,
            "output_tokens": 30,
            "total_tokens": 150,
        },
    )

    normalized = extract_token_usage(response)
    _, usage, limits, reason = account_agent_response(
        {"run_usage": empty_run_usage()},
        _config(),
        response,
    )

    assert normalized == (120, 30, 150, True)
    assert usage["agent_steps"] == 1
    assert usage["total_tokens"] == 150
    assert usage["unmetered_steps"] == 0
    assert limits["max_agent_steps"] == 8
    assert reason is None


def test_tool_budget_discards_over_limit_tool_intent():
    response = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "read_code_file",
                "args": {"file_path": "CodeSmells/main.py"},
                "id": "read-1",
                "type": "tool_call",
            },
            {
                "name": "read_code_file",
                "args": {"file_path": "CodeSmells/models.py"},
                "id": "read-2",
                "type": "tool_call",
            },
        ],
    )

    safe_response, usage, _, reason = account_agent_response(
        {"run_usage": empty_run_usage()},
        _config(max_tool_calls=1),
        response,
    )

    assert usage["tool_calls"] == 2
    assert reason is not None
    assert "超过上限" in reason
    assert safe_response.tool_calls == []
    assert "RUN_BUDGET_EXCEEDED" in safe_response.content


def test_tool_limit_still_allows_a_final_response_without_new_tools():
    usage = empty_run_usage()
    usage["tool_calls"] = 1

    _, next_usage, _, reason = account_agent_response(
        {"run_usage": usage},
        _config(max_tool_calls=1),
        AIMessage(content="工具结果总结"),
    )

    assert next_usage["tool_calls"] == 1
    assert reason is None


def test_unmetered_provider_still_has_step_preflight_guard():
    response = AIMessage(content="供应商没有返回 usage")
    _, usage, _, reason = account_agent_response(
        {"run_usage": empty_run_usage()},
        _config(max_agent_steps=3),
        response,
    )

    assert reason is None
    assert usage["unmetered_steps"] == 1
    usage["agent_steps"] = 3
    assert "Agent 步数" in (
        budget_preflight_reason({"run_usage": usage}, _config(max_agent_steps=3)) or ""
    )


def test_node_preflight_does_not_invoke_model_after_budget_is_exhausted(monkeypatch):
    class DummyLLM:
        def bind_tools(self, _tools):
            return self

        def invoke(self, _messages):
            raise AssertionError("预算预检应在模型调用前终止")

    monkeypatch.setattr("agent.nodes.get_llm_from_config", lambda _cfg: DummyLLM())
    usage = empty_run_usage()
    usage["agent_steps"] = 3

    result = call_developer(
        {
            "messages": [HumanMessage(content="继续重构")],
            "retry_count": 0,
            "run_usage": usage,
        },
        _config(max_agent_steps=3),
    )

    assert result["budget_exceeded"] is True
    assert result["run_usage"]["agent_steps"] == 3
    assert "RUN_BUDGET_EXCEEDED" in result["messages"][0].content


def test_model_timeout_budget_is_passed_to_provider_client(monkeypatch):
    captured = {}
    sentinel = object()

    def fake_openai(**kwargs):
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr("agent.nodes.ChatOpenAI", fake_openai)

    model = get_llm_from_config(_config(model_timeout_seconds=17))

    assert model is sentinel
    assert captured["timeout"] == 17
