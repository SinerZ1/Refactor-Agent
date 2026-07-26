from types import SimpleNamespace

from langchain_core.messages import AIMessage, ToolMessage

import agent
import agent.tools as agent_tools
from agent.events import make_agent_event


def test_event_envelope_keeps_legacy_token_without_hiding_semantics():
    event = make_agent_event(
        "tool.failed",
        "测试未通过",
        level="error",
        node="reviewer",
        tool="run_unit_tests",
        success=False,
        payload={"exit_code": 1},
    )

    assert event["version"] == 1
    assert event["type"] == "tool.failed"
    assert event["success"] is False
    assert event["payload"]["exit_code"] == 1
    assert event["token"].startswith("[ERROR]")


def test_unit_test_tool_marks_nonzero_exit_as_domain_failure(monkeypatch):
    monkeypatch.setattr(
        agent_tools.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            stdout="1 failed", stderr="", returncode=1
        ),
    )

    message = agent_tools.run_unit_tests.invoke(
        {
            "name": "run_unit_tests",
            "args": {"test_suite": "backend"},
            "id": "test-call",
            "type": "tool_call",
        }
    )

    assert isinstance(message, ToolMessage)
    assert message.artifact["success"] is False
    assert message.artifact["exit_code"] == 1


def test_read_tool_marks_rejected_path_as_domain_failure():
    message = agent_tools.read_code_file.invoke(
        {
            "name": "read_code_file",
            "args": {"file_path": "../backend/.env"},
            "id": "read-call",
            "type": "tool_call",
        }
    )

    assert isinstance(message, ToolMessage)
    assert message.artifact["success"] is False
    assert message.artifact["failure_kind"] == "read_error"


def test_stream_translates_tool_artifact_to_failed_event(monkeypatch):
    failed_message = ToolMessage(
        content="测试执行完成。退出代码 (Exit Code): 1",
        tool_call_id="test-call",
        name="run_unit_tests",
        status="success",
        artifact={"success": False, "exit_code": 1},
    )

    class FakeGraph:
        def get_state(self, _config):
            return SimpleNamespace(values={}, interrupts=())

        def stream(self, *_args, **_kwargs):
            yield {"reviewer_tools": {"messages": [failed_message]}}

    monkeypatch.setattr(agent, "app_graph", FakeGraph())

    events = list(agent.stream_refactor("CodeSmells/main.py", "event-test"))

    tool_event = next(event for event in events if event["type"] == "tool.failed")
    assert tool_event["tool"] == "run_unit_tests"
    assert tool_event["success"] is False
    assert tool_event["payload"]["exit_code"] == 1


def test_stream_emits_plan_and_correlates_file_tool_to_dynamic_task(monkeypatch):
    plan = {
        "version": 1,
        "summary": "调整模型",
        "tasks": [
            {
                "id": "domain_models",
                "title": "整理模型",
                "description": "重构数据结构",
                "file_path": "CodeSmells/models.py",
                "dependencies": [],
            }
        ],
    }
    architect_message = AIMessage(content="结构化方案")
    tool_message = ToolMessage(
        content="写入完成",
        tool_call_id="write-call",
        name="write_code_file",
        status="success",
        artifact={"success": True, "file_path": "CodeSmells\\models.py"},
    )

    class FakeGraph:
        def get_state(self, _config):
            return SimpleNamespace(values={}, interrupts=())

        def stream(self, *_args, **_kwargs):
            yield {
                "architect": {
                    "messages": [architect_message],
                    "refactor_plan": plan,
                    "plan_error": None,
                }
            }
            yield {"developer_tools": {"messages": [tool_message]}}

    monkeypatch.setattr(agent, "app_graph", FakeGraph())

    events = list(agent.stream_refactor("CodeSmells/models.py", "plan-event-test"))

    plan_event = next(event for event in events if event["type"] == "plan.created")
    tool_event = next(event for event in events if event["type"] == "tool.completed")
    assert plan_event["payload"]["plan"] == plan
    assert tool_event["task_id"] == "domain_models"
