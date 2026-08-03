# ruff: noqa: E402 -- offline sentinel 必须先于 agent 包导入，阻断 dotenv/Redis 副作用。

from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import MemorySaver

# 必须先设置再导入 ``agent`` 包，阻断 workflow 模块的 dotenv/Redis 初始化副作用。
_previous_offline_import = os.environ.get("REFACTOR_OFFLINE_EVAL_IMPORT")
os.environ["REFACTOR_OFFLINE_EVAL_IMPORT"] = "1"

import agent.plans as agent_plans
import agent.tools as agent_tools
import agent.workspace as agent_workspace
import code_indexer
import graph_indexer
from agent import stream_refactor
from agent.workflow import build_agent_graph

if _previous_offline_import is None:
    os.environ.pop("REFACTOR_OFFLINE_EVAL_IMPORT", None)
else:
    os.environ["REFACTOR_OFFLINE_EVAL_IMPORT"] = _previous_offline_import

from .models import ScriptedFakeModel
from .schema import EvaluationScenario, ModelOutcome


class _TestScript:
    def __init__(self, results: tuple[bool, ...]) -> None:
        self._results = list(results)
        self.calls = 0

    def __call__(self, command: list[str], **_kwargs: Any):
        if "-m" not in command or "pytest" not in command:
            raise AssertionError("Reviewer 测试工具偏离固定 pytest argv")
        if not self._results:
            raise AssertionError("控制面执行了脚本未声明的测试调用")
        self.calls += 1
        passed = self._results.pop(0)
        return subprocess.CompletedProcess(
            command,
            0 if passed else 1,
            stdout="1 passed" if passed else "1 failed",
            stderr="",
        )

    def assert_consumed(self) -> None:
        if self._results:
            raise AssertionError(f"测试脚本未消费完: {len(self._results)}")


@contextmanager
def _isolated_runtime(
    root: Path,
    test_script: _TestScript,
) -> Iterator[None]:
    """把生产文件/测试/基础设施端口指向单场景临时根目录。

    这里没有复制图逻辑：只是替换用户明确允许注入的源码根、测试进程与可选基础
    设施。所有模块常量都会在 finally 恢复，两个场景无法共享工作区或索引状态。
    """

    code_root = (root / "CodeSmells").resolve()
    workspace_root = (root / ".refactor-workspaces").resolve()
    saved: dict[str, Any] = {
        "workspace": (
            agent_workspace.PROJECT_ROOT,
            agent_workspace.REFACTOR_ROOT,
            agent_workspace.WORKSPACE_ROOT,
        ),
        "tools": (agent_tools.PROJECT_ROOT, agent_tools.REFACTOR_ROOT),
        "plans": (agent_plans.PROJECT_ROOT, agent_plans.REFACTOR_ROOT),
        "subprocess_run": agent_tools.subprocess.run,
        "index_directory": code_indexer.index_directory,
        "index_to_neo4j": graph_indexer.index_to_neo4j,
        "get_topology_data": graph_indexer.get_topology_data,
    }
    agent_workspace.PROJECT_ROOT = root
    agent_workspace.REFACTOR_ROOT = code_root
    agent_workspace.WORKSPACE_ROOT = workspace_root
    agent_tools.PROJECT_ROOT = root
    agent_tools.REFACTOR_ROOT = code_root
    agent_plans.PROJECT_ROOT = root
    agent_plans.REFACTOR_ROOT = code_root
    setattr(agent_tools.subprocess, "run", test_script)
    setattr(code_indexer, "index_directory", lambda _root: None)
    setattr(graph_indexer, "index_to_neo4j", lambda: None)
    setattr(
        graph_indexer,
        "get_topology_data",
        lambda: {"nodes": [], "links": [], "fallback": True},
    )
    try:
        yield
    finally:
        (
            agent_workspace.PROJECT_ROOT,
            agent_workspace.REFACTOR_ROOT,
            agent_workspace.WORKSPACE_ROOT,
        ) = saved["workspace"]
        agent_tools.PROJECT_ROOT, agent_tools.REFACTOR_ROOT = saved["tools"]
        agent_plans.PROJECT_ROOT, agent_plans.REFACTOR_ROOT = saved["plans"]
        setattr(agent_tools.subprocess, "run", saved["subprocess_run"])
        setattr(code_indexer, "index_directory", saved["index_directory"])
        setattr(graph_indexer, "index_to_neo4j", saved["index_to_neo4j"])
        setattr(graph_indexer, "get_topology_data", saved["get_topology_data"])


def _write_fixture(root: Path, files: dict[str, str]) -> None:
    for relative, content in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def _sanitized_apply_failure(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    artifacts = value.get("recovery_artifacts", [])
    return {
        "code": str(value.get("code") or "workspace_error"),
        "phase": str(value.get("phase") or "unknown"),
        "conflicts": [
            str(item).replace("\\", "/") for item in value.get("conflicts", [])
        ],
        "rollback_status": str(value.get("rollback_status") or "not_started"),
        "recovery_artifact_ids": [Path(str(item)).name for item in artifacts],
    }


def _terminal_status(
    values: dict[str, Any],
    *,
    approval_requested: bool,
    approval: bool | None,
    safety_blocked: bool,
) -> str:
    if values.get("workspace_apply_failure"):
        return "workspace_conflict"
    if values.get("workspace_applied"):
        return "success"
    if approval_requested and approval is False:
        return "approval_rejected"
    if approval_requested and approval is True and not values.get("workspace_applied"):
        return "workspace_conflict"
    if values.get("budget_exceeded"):
        return "budget_exceeded"
    if safety_blocked:
        return "safety_blocked"
    return "test_failure"


def run_control_plane_scenario(scenario: EvaluationScenario) -> ModelOutcome:
    """运行生产图并从最终 Graph State 与权威事件采集实际结果。"""

    with tempfile.TemporaryDirectory(prefix="refactor-eval-") as temp_dir:
        root = Path(temp_dir).resolve()
        _write_fixture(root, scenario.input.source_files)
        fake_model = ScriptedFakeModel(scenario.script)
        test_script = _TestScript(scenario.input.test_results)
        with _isolated_runtime(root, test_script):
            graph = build_agent_graph(
                checkpointer=MemorySaver(),
                model_resolver=lambda _config: fake_model,
            )
            config = cast(
                RunnableConfig,
                {
                    "configurable": {
                        "thread_id": f"eval-{scenario.id}",
                        "run_budget": scenario.input.run_budget,
                    },
                    "recursion_limit": 300,
                },
            )
            events = list(
                stream_refactor(
                    scenario.input.request,
                    f"eval-{scenario.id}",
                    config,
                    graph=graph,
                )
            )
            snapshot = graph.get_state(config)
            approval_requested = bool(snapshot.interrupts)
            if approval_requested:
                workspace_id = str(snapshot.values.get("workspace_id") or "")
                mutation = scenario.input.mutation_before_resume
                if mutation == "working_tamper":
                    target = agent_workspace.resolve_workspace_path(
                        workspace_id, "CodeSmells/example.py"
                    )
                    target.write_text(
                        "def value():\n    return 999\n", encoding="utf-8"
                    )
                elif mutation == "baseline_conflict":
                    (root / "CodeSmells" / "example.py").write_text(
                        "def value():\n    return 777\n", encoding="utf-8"
                    )
                resume_config = cast(
                    RunnableConfig,
                    {
                        **config,
                        "configurable": {
                            **config["configurable"],
                            "resume_value": {"approved": scenario.input.approval},
                        },
                    },
                )
                events.extend(
                    stream_refactor(
                        scenario.input.request,
                        f"eval-{scenario.id}",
                        resume_config,
                        graph=graph,
                    )
                )
                config = resume_config
                snapshot = graph.get_state(config)

            fake_model.assert_consumed()
            test_script.assert_consumed()
            values = dict(snapshot.values)
            event_types = tuple(str(event["type"]) for event in events)
            tool_failures = [
                event
                for event in events
                if event["type"] == "tool.failed"
                and isinstance(event.get("payload"), dict)
            ]
            safety_blocked = any(
                event["payload"].get("failure_kind")
                in {
                    "task_path_violation",
                    "write_error",
                    "missing_active_task",
                }
                for event in tool_failures
            )
            review_results = tuple(
                event["type"] == "review.passed"
                for event in events
                if event["type"] in {"review.passed", "review.failed"}
            )
            plan = values.get("refactor_plan")
            planned_files = (
                tuple(task["file_path"] for task in plan["tasks"])
                if isinstance(plan, dict)
                else ()
            )
            change_records = values.get("change_records", [])
            recorded_modified = tuple(
                dict.fromkeys(str(record["file_path"]) for record in change_records)
            )
            modified_files = tuple(
                values.get("workspace_changed_files") or recorded_modified
            )
            applied_files = modified_files if values.get("workspace_applied") else ()
            test_records = values.get("test_run_records", [])
            tests_passed = bool(test_records and test_records[-1].get("success"))
            usage = values.get("run_usage", {})
            workspace_root = root / ".refactor-workspaces"
            workspace_cleaned = bool(values.get("workspace_cleaned")) and not any(
                workspace_root.iterdir() if workspace_root.is_dir() else ()
            )
            terminal_status = _terminal_status(
                values,
                approval_requested=approval_requested,
                approval=scenario.input.approval,
                safety_blocked=safety_blocked,
            )
            return ModelOutcome(
                terminal_status=terminal_status,  # type: ignore[arg-type]
                planned_files=planned_files,
                modified_files=modified_files,
                applied_files=applied_files,
                review_results=review_results,
                retry_count=int(values.get("retry_count") or 0),
                tool_calls=int(usage.get("tool_calls") or 0),
                input_tokens=int(usage.get("input_tokens") or 0),
                output_tokens=int(usage.get("output_tokens") or 0),
                tests_passed=tests_passed,
                approval_requested=approval_requested,
                approval_granted=(
                    scenario.input.approval if approval_requested else None
                ),
                safety_blocked=safety_blocked,
                budget_exceeded=bool(values.get("budget_exceeded")),
                plan_status=str(values.get("plan_status") or "pending"),
                task_statuses=tuple(
                    sorted(
                        (str(key), str(value))
                        for key, value in values.get("task_statuses", {}).items()
                    )
                ),
                workspace_cleaned=workspace_cleaned,
                event_types=event_types,
                workspace_apply_failure=_sanitized_apply_failure(
                    values.get("workspace_apply_failure")
                ),
            )
