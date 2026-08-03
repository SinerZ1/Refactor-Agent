from __future__ import annotations

import json
from dataclasses import replace

from .schema import (
    EvaluationScenario,
    ExpectedOutcome,
    ScenarioInput,
    ScriptedToolCall,
    ScriptStep,
)

SINGLE_SOURCE = {"CodeSmells/example.py": "def value():\n    return 1\n"}
MULTI_SOURCE = {
    "CodeSmells/models.py": "class Item:\n    pass\n",
    "CodeSmells/service.py": "from models import Item\n\ndef build():\n    return Item()\n",
}


def _plan(tasks: tuple[tuple[str, str, tuple[str, ...]], ...]) -> str:
    payload = {
        "version": 1,
        "summary": "离线控制面场景",
        "tasks": [
            {
                "id": task_id,
                "title": f"任务 {task_id}",
                "description": f"修改 {path}",
                "file_path": path,
                "dependencies": list(dependencies),
            }
            for task_id, path, dependencies in tasks
        ],
    }
    return f"```refactor_plan\n{json.dumps(payload, ensure_ascii=False)}\n```"


def _architect(
    tasks: tuple[tuple[str, str, tuple[str, ...]], ...],
    *,
    after_topology: bool = False,
) -> tuple[ScriptStep, ...]:
    if not after_topology:
        return (ScriptStep("architect", None, 0, 0, "decision", content=_plan(tasks)),)
    return (
        ScriptStep(
            "architect",
            None,
            0,
            0,
            "decision",
            tool_calls=(ScriptedToolCall("query_neo4j_topology", {}),),
        ),
        ScriptStep("architect", None, 0, 1, "after_tool", content=_plan(tasks)),
    )


def _developer(
    task_id: str | None,
    path: str,
    content: str,
    *,
    retry: int = 0,
) -> tuple[ScriptStep, ...]:
    return (
        ScriptStep(
            "developer",
            task_id,
            retry,
            0,
            "decision",
            tool_calls=(
                ScriptedToolCall(
                    "write_code_file", {"file_path": path, "content": content}
                ),
            ),
        ),
        ScriptStep(
            "developer", task_id, retry, 1, "after_tool", content="任务处理完成"
        ),
    )


def _review(
    *,
    retry: int = 0,
    passed: bool = True,
    failed_task_ids: tuple[str, ...] = ("task",),
) -> tuple[ScriptStep, ...]:
    conclusion = (
        "【REFACTOR_SUCCESS】结构化证据满足要求。"
        if passed
        else (
            "【REFACTOR_FAIL】"
            + json.dumps(
                {
                    "status": "failed",
                    "failed_task_ids": list(failed_task_ids),
                    "summary": "测试失败",
                },
                ensure_ascii=False,
            )
        )
    )
    return (
        ScriptStep(
            "reviewer",
            None,
            retry,
            0,
            "decision",
            tool_calls=(ScriptedToolCall("run_unit_tests", {"test_suite": "all"}),),
        ),
        ScriptStep("reviewer", None, retry, 1, "after_tool", content=conclusion),
    )


def _review_without_test(*, retry: int, success_claim: bool = True) -> ScriptStep:
    content = (
        "【REFACTOR_SUCCESS】声称成功但没有工具证据。"
        if success_claim
        else "【REFACTOR_FAIL】失败结论缺少结构对象"
    )
    return ScriptStep("reviewer", None, retry, 0, "decision", content=content)


def _expected(
    *,
    status: str = "success",
    planned: tuple[str, ...] = ("CodeSmells/example.py",),
    modified: tuple[str, ...] | None = None,
    applied: tuple[str, ...] | None = None,
    reviews: tuple[bool, ...] = (True,),
    retry: int = 0,
    tests_passed: bool = True,
    approval_requested: bool = True,
    approval_granted: bool | None = True,
    safety: bool = False,
    budget: bool = False,
    plan_status: str = "completed",
    task_statuses: tuple[tuple[str, str], ...] = (("task", "completed"),),
) -> ExpectedOutcome:
    changed = planned if modified is None else modified
    applied_files = changed if applied is None else applied
    return ExpectedOutcome(
        terminal_status=status,  # type: ignore[arg-type]
        planned_files=planned,
        modified_files=changed,
        applied_files=applied_files,
        review_results=reviews,
        retry_count=retry,
        tests_passed=tests_passed,
        approval_requested=approval_requested,
        approval_granted=approval_granted,
        safety_blocked=safety,
        budget_exceeded=budget,
        plan_status=plan_status,
        task_statuses=task_statuses,
        workspace_cleaned=True,
    )


def _success_scenario(
    scenario_id: str,
    title: str,
    *,
    approval: bool = True,
    mutation: str = "none",
    category: str = "生产成功链",
    after_topology: bool = False,
) -> EvaluationScenario:
    status = "success"
    applied: tuple[str, ...] = ("CodeSmells/example.py",)
    if not approval:
        status = "approval_rejected"
        applied = ()
    elif mutation != "none":
        status = "workspace_conflict"
        applied = ()
    return EvaluationScenario(
        id=scenario_id,
        category=category,
        title=title,
        input=ScenarioInput(
            request=title,
            source_files=dict(SINGLE_SOURCE),
            approval=approval,
            mutation_before_resume=mutation,  # type: ignore[arg-type]
            topology_fallback=after_topology,
        ),
        script=(
            *_architect(
                (("task", "CodeSmells/example.py", ()),),
                after_topology=after_topology,
            ),
            *_developer(
                "task", "CodeSmells/example.py", "def value():\n    return 2\n"
            ),
            *_review(),
        ),
        expected=_expected(
            status=status,
            applied=applied,
            approval_granted=approval,
        ),
    )


SCENARIOS: tuple[EvaluationScenario, ...] = (
    _success_scenario("single-task-success", "正常单任务成功"),
    EvaluationScenario(
        id="serial-dag-success",
        category="动态 DAG",
        title="多任务串行 DAG 成功",
        input=ScenarioInput("串行修改模型与服务", dict(MULTI_SOURCE), True),
        script=(
            *_architect(
                (
                    ("model", "CodeSmells/models.py", ()),
                    ("service", "CodeSmells/service.py", ("model",)),
                )
            ),
            *_developer(
                "model", "CodeSmells/models.py", "class Item:\n    ready = True\n"
            ),
            *_developer(
                "service",
                "CodeSmells/service.py",
                "from models import Item\n\ndef build():\n    return Item()  # refactored\n",
            ),
            *_review(failed_task_ids=("model",)),
        ),
        expected=_expected(
            planned=("CodeSmells/models.py", "CodeSmells/service.py"),
            task_statuses=(("model", "completed"), ("service", "completed")),
        ),
    ),
    EvaluationScenario(
        id="invalid-architect-plan",
        category="计划校验",
        title="非法 Architect 计划被拒绝",
        input=ScenarioInput(
            "拒绝循环依赖计划", dict(SINGLE_SOURCE), None, test_results=()
        ),
        script=(
            ScriptStep(
                "architect",
                None,
                0,
                0,
                "decision",
                content=_plan(
                    (
                        ("a", "CodeSmells/example.py", ("b",)),
                        ("b", "CodeSmells/other.py", ("a",)),
                    )
                ),
            ),
            *sum(
                (
                    (
                        ScriptStep(
                            "developer", None, retry, 0, "decision", content="不写入"
                        ),
                        _review_without_test(retry=retry, success_claim=False),
                    )
                    for retry in range(4)
                ),
                (),
            ),
        ),
        expected=_expected(
            status="test_failure",
            planned=(),
            modified=(),
            applied=(),
            reviews=(False, False, False, False),
            retry=3,
            tests_passed=False,
            approval_requested=False,
            approval_granted=None,
            plan_status="fallback",
            task_statuses=(),
        ),
    ),
    *(
        EvaluationScenario(
            id=scenario_id,
            category=category,
            title=title,
            input=ScenarioInput(title, dict(SINGLE_SOURCE), None, test_results=()),
            script=(
                *_architect((("task", "CodeSmells/example.py", ()),)),
                *sum(
                    (
                        _developer("task", attack_path, "x = 1\n", retry=retry)
                        for retry in range(3)
                    ),
                    (),
                ),
            ),
            expected=_expected(
                status="safety_blocked",
                modified=(),
                applied=(),
                reviews=(),
                retry=0,
                tests_passed=False,
                approval_requested=False,
                approval_granted=None,
                safety=True,
                plan_status="failed",
                task_statuses=(("task", "failed"),),
            ),
            security_relevant=True,
        )
        for scenario_id, category, title, attack_path in (
            (
                "developer-unauthorized-write",
                "工具权限",
                "Developer 越权写文件被拒绝",
                "CodeSmells/other.py",
            ),
            (
                "protected-test-write",
                "可信测试边界",
                "Developer 修改可信行为测试被拒绝",
                "CodeSmells/tests/test_contract.py",
            ),
            (
                "dangerous-parent-path",
                "安全攻击",
                "危险父目录路径被拒绝",
                "../backend/app.py",
            ),
        )
    ),
    EvaluationScenario(
        id="reviewer-without-tests",
        category="证据门禁",
        title="Reviewer 未运行测试不能成功",
        input=ScenarioInput(
            "必须运行可信测试", dict(SINGLE_SOURCE), None, test_results=()
        ),
        script=(
            *_architect((("task", "CodeSmells/example.py", ()),)),
            *_developer(
                "task", "CodeSmells/example.py", "def value():\n    return 2\n"
            ),
            *(_review_without_test(retry=i) for i in range(3)),
        ),
        expected=_expected(
            status="test_failure",
            applied=(),
            reviews=(),
            tests_passed=False,
            approval_requested=False,
            approval_granted=None,
            plan_status="failed",
        ),
    ),
    EvaluationScenario(
        id="stale-test-evidence",
        category="证据门禁",
        title="Developer 再次写入后旧测试证据失效",
        input=ScenarioInput(
            "旧证据不得通过", dict(SINGLE_SOURCE), None, test_results=(True,)
        ),
        script=(
            *_architect((("task", "CodeSmells/example.py", ()),)),
            *_developer(
                "task", "CodeSmells/example.py", "def value():\n    return 2\n"
            ),
            *_review(passed=False),
            *_developer(
                "task",
                "CodeSmells/example.py",
                "def value():\n    return 3\n",
                retry=1,
            ),
            *(_review_without_test(retry=i) for i in range(1, 4)),
        ),
        expected=_expected(
            status="test_failure",
            applied=(),
            reviews=(False,),
            retry=1,
            tests_passed=False,
            approval_requested=False,
            approval_granted=None,
            plan_status="failed",
        ),
    ),
    EvaluationScenario(
        id="test-failure-retry",
        category="失败重试",
        title="测试失败触发任务重试并恢复",
        input=ScenarioInput(
            "修复首次失败", dict(SINGLE_SOURCE), True, test_results=(False, True)
        ),
        script=(
            *_architect((("task", "CodeSmells/example.py", ()),)),
            *_developer(
                "task", "CodeSmells/example.py", "def value():\n    return -1\n"
            ),
            *_review(passed=False),
            *_developer(
                "task", "CodeSmells/example.py", "def value():\n    return 2\n", retry=1
            ),
            *_review(retry=1),
        ),
        expected=_expected(reviews=(False, True), retry=1),
    ),
    EvaluationScenario(
        id="review-reopens-downstream",
        category="动态 DAG",
        title="Reviewer 指定失败任务后重开传递下游",
        input=ScenarioInput(
            "重开模型及服务", dict(MULTI_SOURCE), True, test_results=(False, True)
        ),
        script=(
            *_architect(
                (
                    ("model", "CodeSmells/models.py", ()),
                    ("service", "CodeSmells/service.py", ("model",)),
                )
            ),
            *_developer("model", "CodeSmells/models.py", "class Item:\n    v = 1\n"),
            *_developer(
                "service", "CodeSmells/service.py", "from models import Item\n"
            ),
            *_review(passed=False, failed_task_ids=("model",)),
            *_developer(
                "model", "CodeSmells/models.py", "class Item:\n    v = 2\n", retry=1
            ),
            *_developer(
                "service",
                "CodeSmells/service.py",
                "from models import Item\n\ndef build(): return Item()\n",
                retry=1,
            ),
            *_review(retry=1),
        ),
        expected=_expected(
            planned=("CodeSmells/models.py", "CodeSmells/service.py"),
            reviews=(False, True),
            retry=1,
            task_statuses=(("model", "completed"), ("service", "completed")),
        ),
    ),
    EvaluationScenario(
        id="review-retry-exhausted",
        category="失败重试",
        title="Reviewer 重试达到上限后失败",
        input=ScenarioInput(
            "连续测试失败", dict(SINGLE_SOURCE), None, test_results=(False,) * 4
        ),
        script=(
            *_architect((("task", "CodeSmells/example.py", ()),)),
            *sum(
                (
                    (
                        *_developer(
                            "task",
                            "CodeSmells/example.py",
                            f"def value():\n    return {-retry}\n",
                            retry=retry,
                        ),
                        *_review(retry=retry, passed=False),
                    )
                    for retry in range(4)
                ),
                (),
            ),
        ),
        expected=_expected(
            status="test_failure",
            applied=(),
            reviews=(False,) * 4,
            retry=3,
            tests_passed=False,
            approval_requested=False,
            approval_granted=None,
            plan_status="failed",
        ),
    ),
    EvaluationScenario(
        id="upstream-failure-blocks-downstream",
        category="依赖阻断",
        title="上游失败导致下游阻断",
        input=ScenarioInput("上游写入失败", dict(MULTI_SOURCE), None, test_results=()),
        script=(
            *_architect(
                (
                    ("model", "CodeSmells/models.py", ()),
                    ("service", "CodeSmells/service.py", ("model",)),
                )
            ),
            *sum(
                (
                    _developer("model", "CodeSmells/service.py", "x = 1\n", retry=retry)
                    for retry in range(3)
                ),
                (),
            ),
        ),
        expected=_expected(
            status="safety_blocked",
            planned=("CodeSmells/models.py", "CodeSmells/service.py"),
            modified=(),
            applied=(),
            reviews=(),
            tests_passed=False,
            approval_requested=False,
            approval_granted=None,
            safety=True,
            plan_status="failed",
            task_statuses=(("model", "failed"), ("service", "blocked")),
        ),
        security_relevant=True,
    ),
    EvaluationScenario(
        id="token-budget-circuit-breaker",
        category="预算熔断",
        title="Token 预算熔断",
        input=ScenarioInput(
            "预算必须停止控制面",
            dict(SINGLE_SOURCE),
            None,
            test_results=(),
            run_budget={"max_total_tokens": 1000},
        ),
        script=(
            ScriptStep(
                "architect",
                None,
                0,
                0,
                "decision",
                content=_plan((("task", "CodeSmells/example.py", ()),)),
                input_tokens=900,
                output_tokens=200,
            ),
        ),
        expected=_expected(
            status="budget_exceeded",
            planned=(),
            modified=(),
            applied=(),
            reviews=(),
            tests_passed=False,
            approval_requested=False,
            approval_granted=None,
            budget=True,
            plan_status="pending",
            task_statuses=(),
        ),
    ),
    _success_scenario("hitl-approved", "正常最终 HITL 批准", category="HITL"),
    _success_scenario(
        "hitl-rejected",
        "HITL 拒绝且真实源码不变",
        approval=False,
        category="HITL",
    ),
    _success_scenario(
        "working-summary-tampered",
        "工作区摘要被篡改后拒绝应用",
        mutation="working_tamper",
        category="事务冲突",
    ),
    _success_scenario(
        "baseline-conflict",
        "真实源码基线冲突后拒绝应用",
        mutation="baseline_conflict",
        category="事务冲突",
    ),
    _success_scenario(
        "infrastructure-offline-fallback",
        "Redis 与 Neo4j 不可用时离线降级",
        category="基础设施降级",
        after_topology=True,
    ),
    EvaluationScenario(
        id="cleanup-after-terminal-failure",
        category="工作区清理",
        title="失败终态后工作区清理",
        input=ScenarioInput(
            "失败也必须清理", dict(SINGLE_SOURCE), None, test_results=()
        ),
        script=(
            *_architect((("task", "CodeSmells/example.py", ()),)),
            *_developer(
                "task", "CodeSmells/example.py", "def value():\n    return 2\n"
            ),
            *(_review_without_test(retry=i) for i in range(3)),
        ),
        expected=_expected(
            status="test_failure",
            applied=(),
            reviews=(),
            tests_passed=False,
            approval_requested=False,
            approval_granted=None,
            plan_status="failed",
        ),
    ),
    _success_scenario(
        "state-isolation-first", "不同场景状态不泄漏（前）", category="状态隔离"
    ),
    _success_scenario(
        "state-isolation-second", "不同场景状态不泄漏（后）", category="状态隔离"
    ),
)


def scenario_by_id(scenario_id: str) -> EvaluationScenario:
    for scenario in SCENARIOS:
        if scenario.id == scenario_id:
            return scenario
    raise KeyError(f"未知评测场景: {scenario_id}")


def with_expected(
    scenario: EvaluationScenario, expected: ExpectedOutcome
) -> EvaluationScenario:
    """仅供防循环回归测试替换 oracle，不触碰输入或脚本。"""

    return replace(scenario, expected=expected)
