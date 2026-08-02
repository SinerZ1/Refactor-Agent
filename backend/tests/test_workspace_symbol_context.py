import json

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

import agent.scheduler as agent_scheduler
import agent.tools as agent_tools
import agent.workspace as agent_workspace
import code_indexer
import graph_indexer
from agent.scheduler import (
    complete_active_task_node,
    initialize_plan_execution,
    render_dependency_context,
    reopen_tasks_after_review,
    schedule_next_task_node,
)
from agent.state import State


def _task(task_id, file_path, dependencies=None):
    return {
        "id": task_id,
        "title": f"任务 {task_id}",
        "description": f"完成 {task_id}",
        "file_path": file_path,
        "dependencies": dependencies or [],
    }


def _plan(*tasks):
    return {"version": 1, "summary": "工作区索引测试", "tasks": list(tasks)}


@pytest.fixture
def indexed_project(tmp_path, monkeypatch):
    original_global = dict(code_indexer.SYMBOL_INDEX)
    code_root = tmp_path / "CodeSmells"
    code_root.mkdir()
    (code_root / "upstream.py").write_text(
        "def original_symbol():\n    return 'original'\n", encoding="utf-8"
    )
    (code_root / "downstream.py").write_text(
        "def downstream():\n    return None\n", encoding="utf-8"
    )
    (code_root / "unrelated.py").write_text(
        "def unrelated_secret():\n    return 42\n", encoding="utf-8"
    )
    monkeypatch.setattr(agent_tools, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(agent_tools, "REFACTOR_ROOT", code_root)
    monkeypatch.setattr(agent_workspace, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(agent_workspace, "REFACTOR_ROOT", code_root)
    monkeypatch.setattr(
        agent_workspace, "WORKSPACE_ROOT", tmp_path / ".refactor-workspaces"
    )
    code_indexer.index_directory(code_root)
    workspace_states = []

    def create_workspace():
        state = agent_workspace.create_run_workspace()
        workspace_states.append(state)
        return state

    yield tmp_path, code_root, create_workspace
    for state in workspace_states:
        workspace_dir = agent_workspace.WORKSPACE_ROOT / state["workspace_id"]
        if workspace_dir.exists():
            agent_workspace.cleanup_run_workspace(state["workspace_id"])
    code_indexer.SYMBOL_INDEX.clear()
    code_indexer.SYMBOL_INDEX.update(original_global)


def _invoke_tool(tool, state, args, call_id="tool-call"):
    tool_call = {
        "name": tool.name,
        "args": args,
        "id": call_id,
        "type": "tool_call",
    }
    graph_builder = StateGraph(State)
    graph_builder.add_node("tools", ToolNode([tool]))
    graph_builder.add_edge(START, "tools")
    graph_builder.add_edge("tools", END)
    return graph_builder.compile().invoke(
        {
            **state,
            "messages": [AIMessage(content="", tool_calls=[tool_call])],
            "retry_count": 0,
        }
    )


def _dynamic_state(workspace_state, plan, active_task_id):
    return {
        **workspace_state,
        "messages": [],
        "retry_count": 0,
        "refactor_plan": plan,
        "active_task_id": active_task_id,
        "task_statuses": {
            task["id"]: ("running" if task["id"] == active_task_id else "completed")
            for task in plan["tasks"]
        },
        "completed_task_ids": [
            task["id"] for task in plan["tasks"] if task["id"] != active_task_id
        ],
        "task_results": {},
        "change_records": [],
        "test_run_records": [],
    }


def test_downstream_query_sees_new_workspace_symbol_without_mutating_global_index(
    indexed_project,
):
    _root, _code_root, create_workspace = indexed_project
    workspace = create_workspace()
    original_index = dict(code_indexer.SYMBOL_INDEX)
    target = agent_workspace.resolve_workspace_path(
        workspace["workspace_id"], "CodeSmells/upstream.py"
    )
    target.write_text(
        "def workspace_symbol():\n    return 'working'\n", encoding="utf-8"
    )
    plan = _plan(
        _task("upstream", "CodeSmells/upstream.py"),
        _task("downstream", "CodeSmells/downstream.py", ["upstream"]),
    )
    state = _dynamic_state(workspace, plan, "downstream")

    result = _invoke_tool(
        agent_tools.search_workspace_symbol_definition,
        state,
        {"symbol_name": "workspace_symbol"},
    )
    message = result["messages"][-1]

    assert isinstance(message, ToolMessage)
    assert "return 'working'" in message.content
    assert "CodeSmells/upstream.py" in message.content
    assert message.artifact["source_scope"] == "workspace"
    assert code_indexer.SYMBOL_INDEX == original_index
    assert "workspace_symbol" not in code_indexer.get_symbol_definition_content(
        "workspace_symbol"
    ).replace("`workspace_symbol`", "")
    assert "original_symbol" in code_indexer.get_symbol_definition_content(
        "original_symbol"
    )


def test_two_workspace_queries_never_share_symbols(indexed_project):
    _root, _code_root, create_workspace = indexed_project
    first = create_workspace()
    second = create_workspace()
    agent_workspace.resolve_workspace_path(
        first["workspace_id"], "CodeSmells/upstream.py"
    ).write_text("def only_first():\n    return 1\n", encoding="utf-8")
    agent_workspace.resolve_workspace_path(
        second["workspace_id"], "CodeSmells/upstream.py"
    ).write_text("def only_second():\n    return 2\n", encoding="utf-8")
    plan = _plan(_task("upstream", "CodeSmells/upstream.py"))

    first_result = _invoke_tool(
        agent_tools.search_workspace_symbol_definition,
        _dynamic_state(first, plan, "upstream"),
        {"symbol_name": "only_first"},
        "first-run-query",
    )
    second_result = _invoke_tool(
        agent_tools.search_workspace_symbol_definition,
        _dynamic_state(second, plan, "upstream"),
        {"symbol_name": "only_first"},
        "second-run-query",
    )

    assert "def only_first" in first_result["messages"][-1].content
    assert "未能在指定 AST 索引" in second_result["messages"][-1].content


def test_write_tool_is_followed_by_fresh_on_demand_symbol_query(indexed_project):
    _root, _code_root, create_workspace = indexed_project
    workspace = create_workspace()
    plan = _plan(_task("upstream", "CodeSmells/upstream.py"))
    state = _dynamic_state(workspace, plan, "upstream")
    written = _invoke_tool(
        agent_tools.write_code_file,
        state,
        {
            "file_path": "CodeSmells/upstream.py",
            "content": "def written_now():\n    return 'fresh'\n",
        },
        "write-new-symbol",
    )

    queried = _invoke_tool(
        agent_tools.search_workspace_symbol_definition,
        written,
        {"symbol_name": "written_now"},
        "query-new-symbol",
    )

    assert "return 'fresh'" in queried["messages"][-1].content


def test_workspace_index_handles_rename_delete_and_syntax_error(indexed_project):
    _root, _code_root, create_workspace = indexed_project
    workspace = create_workspace()
    source = agent_workspace.resolve_workspace_path(
        workspace["workspace_id"], "CodeSmells/upstream.py"
    )
    renamed = source.with_name("renamed.py")
    source.rename(renamed)
    plan = _plan(
        _task("upstream", "CodeSmells/upstream.py"),
        _task("downstream", "CodeSmells/downstream.py", ["upstream"]),
        _task("renamed", "CodeSmells/renamed.py", ["upstream"]),
    )
    state = _dynamic_state(workspace, plan, "renamed")

    renamed_result = _invoke_tool(
        agent_tools.search_workspace_symbol_definition,
        state,
        {"symbol_name": "original_symbol"},
        "renamed-query",
    )
    assert "CodeSmells/renamed.py" in renamed_result["messages"][-1].content

    renamed.unlink()
    deleted_result = _invoke_tool(
        agent_tools.search_workspace_symbol_definition,
        state,
        {"symbol_name": "original_symbol"},
        "deleted-query",
    )
    assert "未能在指定 AST 索引" in deleted_result["messages"][-1].content

    renamed.write_text("def broken(:\n", encoding="utf-8")
    invalid_result = _invoke_tool(
        agent_tools.search_workspace_symbol_definition,
        state,
        {"symbol_name": "original_symbol"},
        "invalid-query",
    )
    assert "AST_PARSE_WARNINGS" in invalid_result["messages"][-1].content
    assert invalid_result["messages"][-1].artifact["parse_error_count"] == 1


def test_workspace_ast_query_does_not_depend_on_neo4j(indexed_project, monkeypatch):
    _root, _code_root, create_workspace = indexed_project
    workspace = create_workspace()
    plan = _plan(_task("upstream", "CodeSmells/upstream.py"))
    monkeypatch.setattr(
        graph_indexer,
        "get_neo4j_driver",
        lambda: (_ for _ in ()).throw(AssertionError("不应访问 Neo4j")),
    )

    result = _invoke_tool(
        agent_tools.search_workspace_symbol_definition,
        _dynamic_state(workspace, plan, "upstream"),
        {"symbol_name": "original_symbol"},
    )

    assert "def original_symbol" in result["messages"][-1].content
    assert result["messages"][-1].artifact["source_scope"] == "workspace"


def test_completed_task_produces_structured_context_for_downstream(
    indexed_project,
):
    _root, _code_root, create_workspace = indexed_project
    workspace = create_workspace()
    plan = _plan(
        _task("upstream", "CodeSmells/upstream.py"),
        _task("downstream", "CodeSmells/downstream.py", ["upstream"]),
    )
    state = {
        **workspace,
        "messages": [],
        "retry_count": 0,
        "refactor_plan": plan,
        **initialize_plan_execution(plan, None),
    }
    state.update(schedule_next_task_node(state))
    target = agent_workspace.resolve_workspace_path(
        workspace["workspace_id"], "CodeSmells/upstream.py"
    )
    target.write_text("def renamed_api():\n    return 'new'\n", encoding="utf-8")
    state["active_task_write_succeeded"] = True
    state.update(complete_active_task_node(state))
    scheduled = schedule_next_task_node(state)
    message = scheduled["messages"][0].content
    dependency_json = message.split("[DEPENDENCY_RESULTS]\n", 1)[1].split("\n", 1)[0]
    payload = json.loads(dependency_json)
    result = payload["dependencies"][0]

    assert result["task_id"] == "upstream"
    assert result["status"] == "completed"
    assert result["modified_files"] == ["CodeSmells/upstream.py"]
    assert result["symbols"] == ["renamed_api"]
    assert len(result["workspace_snapshot_digest"]) == 64
    assert len(result["content_sha256"]) == 64


def test_dependency_context_has_hard_size_limit():
    dependencies = [f"task_{index}" for index in range(19)]
    task = _task("consumer", "CodeSmells/consumer.py", dependencies)
    results = {
        task_id: {
            "task_id": task_id,
            "status": "completed",
            "modified_files": [f"CodeSmells/{task_id}_{n}.py" for n in range(20)],
            "change_summary": "x" * 5_000,
            "symbols": ["symbol" * 100 for _ in range(30)],
            "workspace_snapshot_digest": "a" * 64,
            "content_sha256": "b" * 64,
            "syntax_status": "valid",
        }
        for task_id in dependencies
    }
    state = {
        "task_results": results,
        "task_statuses": {task_id: "completed" for task_id in dependencies},
    }

    rendered = render_dependency_context(state, task)
    payload = json.loads(rendered)

    assert len(rendered) <= agent_scheduler.MAX_DEPENDENCY_CONTEXT_CHARS
    assert payload["truncated"] is True
    assert payload["omitted_count"] > 0


def test_dynamic_task_cannot_read_or_query_unrelated_plan_file(indexed_project):
    _root, _code_root, create_workspace = indexed_project
    workspace = create_workspace()
    plan = _plan(
        _task("upstream", "CodeSmells/upstream.py"),
        _task("downstream", "CodeSmells/downstream.py", ["upstream"]),
        _task("unrelated", "CodeSmells/unrelated.py"),
    )
    state = _dynamic_state(workspace, plan, "downstream")

    read_result = _invoke_tool(
        agent_tools.read_code_file,
        state,
        {"file_path": "CodeSmells/unrelated.py"},
        "unauthorized-read",
    )
    search_result = _invoke_tool(
        agent_tools.search_workspace_symbol_definition,
        state,
        {"symbol_name": "unrelated_secret"},
        "unauthorized-search",
    )

    assert read_result["messages"][-1].artifact["success"] is False
    assert read_result["messages"][-1].artifact["failure_kind"] == (
        "task_path_violation"
    )
    assert "未能在指定 AST 索引" in search_result["messages"][-1].content


def test_reviewer_tool_permissions_remain_test_only():
    assert [tool.name for tool in agent_tools.reviewer_tools] == ["run_unit_tests"]
    assert [tool.name for tool in agent_tools.architect_tools].count(
        "search_symbol_definition"
    ) == 1
    assert [tool.name for tool in agent_tools.developer_tools].count(
        "search_symbol_definition"
    ) == 1
    assert "read_code_file" not in {tool.name for tool in agent_tools.reviewer_tools}


def test_reviewer_reopen_and_resumed_retry_use_current_workspace(indexed_project):
    _root, _code_root, create_workspace = indexed_project
    workspace = create_workspace()
    plan = _plan(
        _task("upstream", "CodeSmells/upstream.py"),
        _task("downstream", "CodeSmells/downstream.py", ["upstream"]),
    )
    agent_workspace.resolve_workspace_path(
        workspace["workspace_id"], "CodeSmells/upstream.py"
    ).write_text(
        "def current_retry_symbol():\n    return 'checkpoint-working-tree'\n",
        encoding="utf-8",
    )
    prior_results = {
        task_id: {
            "task_id": task_id,
            "status": "completed",
            "modified_files": [f"CodeSmells/{task_id}.py"],
            "change_summary": "stale",
            "symbols": ["stale_symbol"],
            "workspace_snapshot_digest": "0" * 64,
            "content_sha256": "1" * 64,
            "syntax_status": "valid",
        }
        for task_id in ("upstream", "downstream")
    }
    state = {
        **workspace,
        "messages": [
            AIMessage(
                content=(
                    "【REFACTOR_FAIL】\n"
                    '{"status":"failed","failed_task_ids":["upstream"],'
                    '"summary":"需要重试"}'
                )
            )
        ],
        "retry_count": 0,
        "refactor_plan": plan,
        "task_statuses": {"upstream": "completed", "downstream": "completed"},
        "completed_task_ids": ["upstream", "downstream"],
        "task_failures": {},
        "task_retry_counts": {"upstream": 0, "downstream": 0},
        "task_results": prior_results,
        "plan_status": "completed",
    }

    reopened = reopen_tasks_after_review(state)
    resumed_state = {**state, **reopened}
    scheduled = schedule_next_task_node(resumed_state)
    resumed_state.update(scheduled)
    queried = _invoke_tool(
        agent_tools.search_workspace_symbol_definition,
        resumed_state,
        {"symbol_name": "current_retry_symbol"},
        "retry-workspace-query",
    )

    assert reopened["task_results"] == {}
    assert scheduled["active_task_id"] == "upstream"
    assert "checkpoint-working-tree" in queried["messages"][-1].content
