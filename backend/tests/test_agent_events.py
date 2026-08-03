from types import SimpleNamespace

from langchain_core.messages import AIMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

import agent
import agent.tools as agent_tools
import agent.workspace as agent_workspace
from agent.events import make_agent_event
from agent.state import State


def _invoke_test_tool(test_suite: str):
    tool_call = {
        "name": "run_unit_tests",
        "args": {"test_suite": test_suite},
        "id": "test-call",
        "type": "tool_call",
    }
    graph_builder = StateGraph(State)
    graph_builder.add_node("tools", ToolNode([agent_tools.run_unit_tests]))
    graph_builder.add_edge(START, "tools")
    graph_builder.add_edge("tools", END)
    graph = graph_builder.compile()
    workspace_state = agent_workspace.create_run_workspace()
    try:
        return graph.invoke(
            {
                "messages": [AIMessage(content="", tool_calls=[tool_call])],
                "retry_count": 0,
                "change_records": [],
                "test_run_records": [],
                **workspace_state,
            }
        )
    finally:
        agent_workspace.cleanup_run_workspace(workspace_state["workspace_id"])


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

    result = _invoke_test_tool("backend")
    message = result["messages"][-1]

    assert isinstance(message, ToolMessage)
    assert message.artifact is not None, message.content
    assert message.artifact["success"] is False
    assert message.artifact["exit_code"] == 1
    assert result["test_run_records"][-1]["success"] is False


def test_read_tool_marks_rejected_path_as_domain_failure():
    tool_call = {
        "name": "read_code_file",
        "args": {"file_path": "../backend/.env"},
        "id": "read-call",
        "type": "tool_call",
    }
    graph_builder = StateGraph(State)
    graph_builder.add_node("tools", ToolNode([agent_tools.read_code_file]))
    graph_builder.add_edge(START, "tools")
    graph_builder.add_edge("tools", END)
    workspace_state = agent_workspace.create_run_workspace()
    message = graph_builder.compile().invoke(
        {
            "messages": [AIMessage(content="", tool_calls=[tool_call])],
            "retry_count": 0,
            **workspace_state,
        }
    )["messages"][-1]
    agent_workspace.cleanup_run_workspace(workspace_state["workspace_id"])

    assert isinstance(message, ToolMessage)
    assert message.artifact is not None, message.content
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


def test_stream_exposes_usage_and_budget_exhaustion_as_structured_events(monkeypatch):
    usage = {
        "agent_steps": 3,
        "tool_calls": 2,
        "input_tokens": 900,
        "output_tokens": 100,
        "total_tokens": 1000,
        "unmetered_steps": 0,
    }
    limits = {
        "max_agent_steps": 3,
        "max_tool_calls": 4,
        "max_total_tokens": 10_000,
        "model_timeout_seconds": 30,
    }

    class FakeGraph:
        def get_state(self, _config):
            return SimpleNamespace(values={}, interrupts=())

        def stream(self, *_args, **_kwargs):
            yield {
                "developer": {
                    "messages": [
                        AIMessage(content="【RUN_BUDGET_EXCEEDED】Agent 步数达到上限")
                    ],
                    "run_usage": usage,
                    "run_budget_limits": limits,
                    "budget_exceeded": True,
                    "budget_reason": "Agent 步数达到上限",
                }
            }

    monkeypatch.setattr(agent, "app_graph", FakeGraph())

    events = list(agent.stream_refactor("继续", "budget-event-test"))

    usage_event = next(
        event for event in events if event["type"] == "run.usage.updated"
    )
    exceeded_event = next(
        event for event in events if event["type"] == "run.budget.exceeded"
    )
    assert usage_event["payload"]["usage"]["total_tokens"] == 1000
    assert exceeded_event["success"] is False
    assert exceeded_event["payload"]["reason"] == "Agent 步数达到上限"


def test_stream_emits_review_pass_only_after_evidence_terminal(monkeypatch):
    evidence = {
        "changed_files": ["CodeSmells/example.py"],
        "change_set_digest": "current-digest",
        "test_suite": "CodeSmells",
        "test_success": True,
        "test_exit_code": 0,
    }

    class FakeGraph:
        def get_state(self, _config):
            return SimpleNamespace(values={}, interrupts=())

        def stream(self, *_args, **_kwargs):
            yield {
                "reviewer": {
                    "messages": [AIMessage(content="【REFACTOR_SUCCESS】模型声明通过")]
                }
            }
            yield {
                "finalize_review_success": {
                    "review_status": "success",
                    "review_evidence": evidence,
                }
            }

    monkeypatch.setattr(agent, "app_graph", FakeGraph())

    events = list(agent.stream_refactor("CodeSmells/main.py", "evidence-event-test"))
    passed_events = [event for event in events if event["type"] == "review.passed"]

    assert len(passed_events) == 1
    assert passed_events[0]["payload"]["evidence"] == evidence


def test_dynamic_task_events_carry_the_authoritative_graph_state(monkeypatch):
    plan = {
        "version": 1,
        "summary": "先模型后入口",
        "tasks": [
            {
                "id": "models",
                "title": "模型",
                "description": "整理模型",
                "file_path": "CodeSmells/models.py",
                "dependencies": [],
            },
            {
                "id": "entrypoint",
                "title": "入口",
                "description": "调整入口",
                "file_path": "CodeSmells/main.py",
                "dependencies": ["models"],
            },
        ],
    }

    class FakeGraph:
        def get_state(self, _config):
            return SimpleNamespace(values={}, interrupts=())

        def stream(self, *_args, **_kwargs):
            yield {
                "architect": {
                    "messages": [AIMessage(content="计划")],
                    "refactor_plan": plan,
                    "plan_error": None,
                    "task_statuses": {"models": "pending", "entrypoint": "pending"},
                    "active_task_id": None,
                    "plan_status": "pending",
                }
            }
            yield {
                "schedule_task": {
                    "task_statuses": {"models": "running", "entrypoint": "pending"},
                    "active_task_id": "models",
                    "plan_status": "running",
                    "task_transition": {
                        "task_id": "models",
                        "status": "running",
                        "retry_count": 0,
                    },
                }
            }
            yield {
                "complete_task": {
                    "task_statuses": {
                        "models": "failed",
                        "entrypoint": "blocked",
                    },
                    "active_task_id": None,
                    "plan_status": "failed",
                    "task_transition": {
                        "task_id": "models",
                        "status": "failed",
                        "reason": "重试耗尽",
                        "retry_count": 3,
                        "blocked_task_ids": ["entrypoint"],
                    },
                }
            }

    monkeypatch.setattr(agent, "app_graph", FakeGraph())

    events = list(agent.stream_refactor("CodeSmells/main.py", "state-event-test"))
    started = next(
        event
        for event in events
        if event["type"] == "task.started" and event["task_id"] == "models"
    )
    failed = next(event for event in events if event["type"] == "task.failed")
    blocked = next(event for event in events if event["type"] == "task.blocked")
    plan_failed = next(event for event in events if event["type"] == "plan.failed")

    assert started["task_id"] == "models"
    assert started["payload"]["task_statuses"]["models"] == "running"
    assert failed["payload"]["task_statuses"]["models"] == "failed"
    assert blocked["task_id"] == "entrypoint"
    assert blocked["payload"]["task_statuses"]["entrypoint"] == "blocked"
    assert plan_failed["payload"]["plan_status"] == "failed"


def test_stream_keeps_legacy_workspace_error_and_adds_safe_apply_failure(monkeypatch):
    class FakeGraph:
        def get_state(self, _config):
            return SimpleNamespace(values={}, interrupts=())

        def stream(self, *_args, **_kwargs):
            yield {
                "apply_workspace": {
                    "workspace_approved": True,
                    "workspace_applied": False,
                    "workspace_rolled_back": False,
                    "workspace_error": "需要人工处理",
                    "workspace_changed_files": ["CodeSmells/example.py"],
                    "workspace_apply_failure": {
                        "code": "atomic_apply_failed",
                        "phase": "rollback",
                        "conflicts": ["CodeSmells/example.py"],
                        "rollback_status": "partial",
                        "recovery_artifacts": [
                            "C:/private/.example.py.refactor-recovery-secret"
                        ],
                    },
                }
            }

    monkeypatch.setattr(agent, "app_graph", FakeGraph())
    events = list(agent.stream_refactor("继续", "apply-failure-test"))

    structured = next(
        event for event in events if event["type"] == "workspace.apply.failed"
    )
    legacy = next(event for event in events if event["type"] == "run.failed")
    failure = structured["payload"]["apply_failure"]
    assert failure["rollback_status"] == "partial"
    assert failure["affected_files"] == ["CodeSmells/example.py"]
    assert failure["recovery_available"] is True
    assert "C:/private" not in str(structured)
    assert legacy["payload"]["workspace_error"] == "需要人工处理"
