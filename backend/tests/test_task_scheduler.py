from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode
from langgraph.types import Command

import agent.nodes as agent_nodes
import agent.tools as agent_tools
from agent.edges import route_task_scheduler
from agent.scheduler import (
    complete_active_task_node,
    initialize_plan_execution,
    reopen_tasks_after_review,
    schedule_next_task_node,
)
from agent.state import State


def _task(task_id: str, file_path: str, dependencies: list[str] | None = None):
    return {
        "id": task_id,
        "title": f"任务 {task_id}",
        "description": f"完成 {task_id}",
        "file_path": file_path,
        "dependencies": dependencies or [],
    }


def _plan(*tasks):
    return {"version": 1, "summary": "测试动态调度", "tasks": list(tasks)}


def _state_for(plan):
    return {
        "messages": [],
        "retry_count": 0,
        "refactor_plan": plan,
        **initialize_plan_execution(plan, None),
    }


def _merge(state, update):
    return {**state, **update}


def test_linear_dependencies_run_in_topological_order():
    plan = _plan(
        _task("models", "CodeSmells/models.py"),
        _task("service", "CodeSmells/service.py", ["models"]),
        _task("entrypoint", "CodeSmells/main.py", ["service"]),
    )
    state = _state_for(plan)

    observed = []
    for _ in plan["tasks"]:
        scheduled = schedule_next_task_node(state)
        observed.append(scheduled["active_task_id"])
        state = _merge(state, scheduled)
        state["active_task_write_succeeded"] = True
        state = _merge(state, complete_active_task_node(state))

    assert observed == ["models", "service", "entrypoint"]
    assert state["completed_task_ids"] == observed
    assert state["plan_status"] == "completed"
    assert route_task_scheduler(state) == "reviewer"


def test_scheduler_never_enters_reviewer_while_a_task_is_still_active():
    plan = _plan(_task("models", "CodeSmells/models.py"))
    state = _merge(_state_for(plan), schedule_next_task_node(_state_for(plan)))

    assert state["task_statuses"]["models"] == "running"
    assert route_task_scheduler(state) == "developer"


def test_same_level_tasks_keep_architect_plan_order():
    plan = _plan(
        _task("second_named", "CodeSmells/z.py"),
        _task("first_named", "CodeSmells/a.py"),
    )
    state = _state_for(plan)

    first = schedule_next_task_node(state)
    state = _merge(state, first)
    state["active_task_write_succeeded"] = True
    state = _merge(state, complete_active_task_node(state))
    second = schedule_next_task_node(state)

    assert first["active_task_id"] == "second_named"
    assert second["active_task_id"] == "first_named"


def test_failed_attempt_is_recorded_before_scheduler_retries_same_task():
    plan = _plan(_task("models", "CodeSmells/models.py"))
    state = _merge(_state_for(plan), schedule_next_task_node(_state_for(plan)))
    state["active_task_failure_reason"] = "模型未调用写工具"

    failed_attempt = complete_active_task_node(state)
    assert failed_attempt["task_statuses"]["models"] == "failed"
    assert failed_attempt["task_retry_counts"]["models"] == 1
    assert failed_attempt["plan_status"] == "running"

    retried = schedule_next_task_node(_merge(state, failed_attempt))
    assert retried["active_task_id"] == "models"
    assert retried["task_statuses"]["models"] == "running"
    assert retried["task_transition"]["retry_count"] == 1


def test_exhausted_task_blocks_all_transitive_downstream_tasks():
    plan = _plan(
        _task("root", "CodeSmells/root.py"),
        _task("child", "CodeSmells/child.py", ["root"]),
        _task("leaf", "CodeSmells/leaf.py", ["child"]),
        _task("independent", "CodeSmells/independent.py"),
    )
    state = _state_for(plan)
    state = _merge(state, schedule_next_task_node(state))
    state["task_retry_counts"] = {"root": 2}
    state["active_task_failure_reason"] = "写入失败"

    result = complete_active_task_node(state)

    assert result["task_statuses"]["root"] == "failed"
    assert result["task_statuses"]["child"] == "blocked"
    assert result["task_statuses"]["leaf"] == "blocked"
    assert result["task_statuses"]["independent"] == "pending"
    assert result["plan_status"] == "failed"
    assert result["task_failures"]["root"] == {
        "reason": "写入失败",
        "failure_kind": "task_incomplete",
        "retry_count": 3,
    }


def test_budget_exhaustion_fails_current_task_and_plan():
    plan = _plan(
        _task("current", "CodeSmells/current.py"),
        _task("downstream", "CodeSmells/downstream.py", ["current"]),
    )
    state = _merge(_state_for(plan), schedule_next_task_node(_state_for(plan)))
    state.update(
        budget_exceeded=True,
        budget_reason="Token 预算耗尽",
        # 即使写工具已返回成功，预算熔断导致任务闭环不完整，仍必须失败。
        active_task_write_succeeded=True,
    )

    result = complete_active_task_node(state)

    assert result["task_statuses"] == {
        "current": "failed",
        "downstream": "blocked",
    }
    assert result["task_failures"]["current"]["failure_kind"] == "budget_exceeded"
    assert result["task_failures"]["current"]["reason"] == "Token 预算耗尽"
    assert result["plan_status"] == "failed"


def test_write_tool_rejects_paths_outside_current_task(tmp_path, monkeypatch):
    code_root = tmp_path / "CodeSmells"
    code_root.mkdir()
    monkeypatch.setattr(agent_tools, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(agent_tools, "REFACTOR_ROOT", code_root)
    plan = _plan(
        _task("models", "CodeSmells/models.py"),
        _task("other", "CodeSmells/other.py"),
    )
    tool_call = {
        "name": "write_code_file",
        "args": {
            "file_path": "CodeSmells/other.py",
            "content": "unsafe = True\n",
        },
        "id": "write-other",
        "type": "tool_call",
    }
    graph_builder = StateGraph(State)
    graph_builder.add_node("tools", ToolNode([agent_tools.write_code_file]))
    graph_builder.add_edge(START, "tools")
    graph_builder.add_edge("tools", END)
    graph = graph_builder.compile()

    result = graph.invoke(
        {
            "messages": [AIMessage(content="", tool_calls=[tool_call])],
            "retry_count": 0,
            "refactor_plan": plan,
            "active_task_id": "models",
            "active_task_write_succeeded": False,
        }
    )

    message = result["messages"][-1]
    assert message.artifact["success"] is False
    assert message.artifact["failure_kind"] == "task_path_violation"
    assert result["active_task_write_succeeded"] is False
    assert not (code_root / "other.py").exists()


def test_hitl_resume_keeps_the_same_active_task(tmp_path, monkeypatch):
    code_root = tmp_path / "CodeSmells"
    code_root.mkdir()
    target = code_root / "models.py"
    target.write_text("old = True\n", encoding="utf-8")
    monkeypatch.setattr(agent_tools, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(agent_tools, "REFACTOR_ROOT", code_root)
    monkeypatch.setattr("code_indexer.index_file", lambda _path: 1)
    monkeypatch.setattr("graph_indexer.index_to_neo4j", lambda: False)
    plan = _plan(_task("models", "CodeSmells/models.py"))
    tool_call = {
        "name": "write_code_file",
        "args": {
            "file_path": "CodeSmells/models.py",
            "content": "new = True\n",
        },
        "id": "write-models",
        "type": "tool_call",
    }
    graph_builder = StateGraph(State)
    graph_builder.add_node("tools", ToolNode([agent_tools.write_code_file]))
    graph_builder.add_edge(START, "tools")
    graph_builder.add_edge("tools", END)
    graph = graph_builder.compile(checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": "hitl-task"}}

    graph.invoke(
        {
            "messages": [AIMessage(content="", tool_calls=[tool_call])],
            "retry_count": 0,
            "refactor_plan": plan,
            "active_task_id": "models",
            "active_task_write_succeeded": False,
        },
        config,
    )
    suspended = graph.get_state(config)

    assert suspended.interrupts
    assert suspended.values["active_task_id"] == "models"

    resumed = graph.invoke(Command(resume={"approved": True}), config)

    assert resumed["active_task_id"] == "models"
    assert resumed["active_task_write_succeeded"] is True
    assert resumed["messages"][-1].artifact["task_id"] == "models"
    assert target.read_text(encoding="utf-8") == "new = True\n"


def test_reviewer_reopens_selected_task_and_transitive_downstream_only():
    plan = _plan(
        _task("models", "CodeSmells/models.py"),
        _task("service", "CodeSmells/service.py", ["models"]),
        _task("entrypoint", "CodeSmells/main.py", ["service"]),
    )
    state = _state_for(plan)
    state.update(
        messages=[
            AIMessage(
                content=(
                    "【REFACTOR_FAIL】测试失败\n"
                    '{"status":"failed","failed_task_ids":["service"],'
                    '"summary":"服务层断言失败"}'
                )
            )
        ],
        task_statuses={
            "models": "completed",
            "service": "completed",
            "entrypoint": "completed",
        },
        completed_task_ids=["models", "service", "entrypoint"],
        plan_status="completed",
    )

    result = agent_nodes.developer_retry_node(state)

    assert result["task_statuses"] == {
        "models": "completed",
        "service": "pending",
        "entrypoint": "pending",
    }
    assert result["completed_task_ids"] == ["models"]
    assert result["plan_status"] == "running"


def test_invalid_reviewer_task_ids_conservatively_reopen_the_whole_plan():
    plan = _plan(
        _task("models", "CodeSmells/models.py"),
        _task("entrypoint", "CodeSmells/main.py", ["models"]),
    )
    state = _state_for(plan)
    state.update(
        messages=[
            AIMessage(
                content=(
                    "【REFACTOR_FAIL】\n"
                    '{"status":"failed","failed_task_ids":["unknown"],'
                    '"summary":"非法任务"}'
                )
            )
        ],
        task_statuses={"models": "completed", "entrypoint": "completed"},
        completed_task_ids=["models", "entrypoint"],
        plan_status="completed",
    )

    result = reopen_tasks_after_review(state)

    assert result["task_statuses"] == {
        "models": "pending",
        "entrypoint": "pending",
    }
    assert result["completed_task_ids"] == []
    assert "计划之外" in result["task_transition"]["reason"]
