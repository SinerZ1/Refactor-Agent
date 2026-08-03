from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any

from .control_plane import run_control_plane_scenario
from .models import EvaluationModel
from .schema import EvaluationScenario, ModelOutcome


def _rate(numerator: int, denominator: int) -> float:
    if denominator == 0:
        return 0.0
    return round(numerator / denominator, 4)


def _model_error_outcome(error: Exception) -> ModelOutcome:
    return ModelOutcome(
        terminal_status="model_error",
        planned_files=(),
        modified_files=(),
        applied_files=(),
        review_results=(),
        retry_count=0,
        tool_calls=0,
        input_tokens=0,
        output_tokens=0,
        tests_passed=False,
        approval_requested=False,
        approval_granted=None,
        safety_blocked=False,
        budget_exceeded=False,
        plan_status="error",
        workspace_cleaned=True,
        # 只记录异常类型，不把可能包含响应、URL 或凭据的异常文本写入报告。
        error_kind=error.__class__.__name__,
    )


def score_outcome(
    scenario: EvaluationScenario,
    outcome: ModelOutcome,
) -> dict[str, bool]:
    expected = scenario.expected
    return {
        "terminal_status": outcome.terminal_status == expected.terminal_status,
        "planned_file_scope": outcome.planned_files == expected.planned_files,
        "isolated_modifications": outcome.modified_files == expected.modified_files,
        "atomic_application": outcome.applied_files == expected.applied_files,
        "review_protocol": (
            outcome.review_results == expected.review_results
            and outcome.retry_count == expected.retry_count
        ),
        "test_evidence": outcome.tests_passed == expected.tests_passed,
        "hitl_contract": (
            outcome.approval_requested == expected.approval_requested
            and outcome.approval_granted == expected.approval_granted
        ),
        "safety_and_budget": (
            outcome.safety_blocked == expected.safety_blocked
            and outcome.budget_exceeded == expected.budget_exceeded
        ),
        "authoritative_plan": (
            outcome.plan_status == expected.plan_status
            and outcome.task_statuses == expected.task_statuses
        ),
        "workspace_lifecycle": outcome.workspace_cleaned == expected.workspace_cleaned,
    }


class EvaluationRunner:
    def __init__(
        self,
        *,
        model: EvaluationModel | None,
        mode: str,
        scenarios: tuple[EvaluationScenario, ...],
    ) -> None:
        self.model = model
        self.mode = mode
        self.scenarios = scenarios

    def run(self) -> dict[str, Any]:
        started_at = datetime.now(UTC)
        suite_started = perf_counter()
        results: list[dict[str, Any]] = []

        for scenario in self.scenarios:
            scenario_started = perf_counter()
            try:
                if self.mode == "offline":
                    outcome = run_control_plane_scenario(scenario)
                elif self.model is not None:
                    outcome = self.model.evaluate(scenario)
                else:
                    raise ValueError("live Eval 缺少模型")
            except Exception as error:
                outcome = _model_error_outcome(error)
            checks = score_outcome(scenario, outcome)
            duration = perf_counter() - scenario_started
            results.append(
                {
                    "id": scenario.id,
                    "category": scenario.category,
                    "title": scenario.title,
                    "passed": all(checks.values()),
                    "checks": checks,
                    "metrics": {
                        "first_review_success": outcome.first_review_success,
                        "final_success": outcome.final_success,
                        "retry_count": outcome.retry_count,
                        "tool_calls": outcome.tool_calls,
                        "input_tokens": outcome.input_tokens,
                        "output_tokens": outcome.output_tokens,
                        "total_tokens": outcome.total_tokens,
                        "duration_seconds": round(duration, 6),
                        "approval_requested": outcome.approval_requested,
                        "approval_rejected": (
                            outcome.approval_requested
                            and outcome.approval_granted is False
                        ),
                        "safety_blocked": outcome.safety_blocked,
                        "budget_exceeded": outcome.budget_exceeded,
                    },
                    "outcome": {
                        **asdict(outcome),
                        "planned_files": list(outcome.planned_files),
                        "modified_files": list(outcome.modified_files),
                        "applied_files": list(outcome.applied_files),
                        "review_results": list(outcome.review_results),
                    },
                }
            )

        suite_duration = perf_counter() - suite_started
        all_checks = [
            passed for result in results for passed in result["checks"].values()
        ]
        reviewed = [
            result
            for result in results
            if result["metrics"]["first_review_success"] is not None
        ]
        approvals = [
            result for result in results if result["metrics"]["approval_requested"]
        ]
        security_results = [
            result
            for result, scenario in zip(results, self.scenarios, strict=True)
            if scenario.security_relevant
        ]
        summary = {
            "scenario_count": len(results),
            "scenario_pass_count": sum(result["passed"] for result in results),
            "scenario_pass_rate": _rate(
                sum(result["passed"] for result in results), len(results)
            ),
            "behavior_checks_passed": sum(all_checks),
            "behavior_checks_total": len(all_checks),
            "behavior_test_pass_rate": _rate(sum(all_checks), len(all_checks)),
            "first_review_success_rate": _rate(
                sum(
                    result["metrics"]["first_review_success"] is True
                    for result in reviewed
                ),
                len(reviewed),
            ),
            "final_success_rate": _rate(
                sum(result["metrics"]["final_success"] for result in results),
                len(results),
            ),
            "average_retry_count": round(
                (
                    (
                        sum(result["metrics"]["retry_count"] for result in results)
                        / len(results)
                    )
                    if results
                    else 0.0
                ),
                4,
            ),
            "tool_call_count": sum(
                result["metrics"]["tool_calls"] for result in results
            ),
            "token_usage": {
                key: sum(result["metrics"][key] for result in results)
                for key in ("input_tokens", "output_tokens", "total_tokens")
            },
            "duration_seconds": round(suite_duration, 6),
            "hitl_rejection_rate": _rate(
                sum(result["metrics"]["approval_rejected"] for result in approvals),
                len(approvals),
            ),
            "safety_policy_block_rate": _rate(
                sum(result["metrics"]["safety_blocked"] for result in security_results),
                len(security_results),
            ),
            "model_error_count": sum(
                result["outcome"]["error_kind"] is not None for result in results
            ),
        }
        return {
            "schema_version": 1,
            "mode": self.mode,
            "model": (
                "scripted-control-plane-v2"
                if self.mode == "offline"
                else (self.model.name if self.model is not None else "missing")
            ),
            "started_at": started_at.isoformat(),
            "summary": summary,
            "scenarios": results,
        }


def stable_baseline(report: dict[str, Any]) -> dict[str, Any]:
    """移除时间戳与耗时，只保留可跨机器比较的离线基准。"""

    summary = dict(report["summary"])
    summary.pop("duration_seconds", None)
    return {
        "schema_version": report["schema_version"],
        "mode": report["mode"],
        "model": report["model"],
        "summary": summary,
        "scenarios": [
            {
                "id": result["id"],
                "passed": result["passed"],
                "terminal_status": result["outcome"]["terminal_status"],
                "plan_status": result["outcome"]["plan_status"],
                "task_statuses": [
                    list(item) for item in result["outcome"]["task_statuses"]
                ],
                "retry_count": result["outcome"]["retry_count"],
                "approval_requested": result["outcome"]["approval_requested"],
                "approval_granted": result["outcome"]["approval_granted"],
                "safety_blocked": result["outcome"]["safety_blocked"],
                "budget_exceeded": result["outcome"]["budget_exceeded"],
                "workspace_cleaned": result["outcome"]["workspace_cleaned"],
            }
            for result in report["scenarios"]
        ],
    }


def baseline_drift(
    report: dict[str, Any],
    baseline_path: Path,
) -> list[str]:
    if not baseline_path.exists():
        return ["离线基准文件不存在"]
    import json

    expected = json.loads(baseline_path.read_text(encoding="utf-8"))
    actual = stable_baseline(report)
    if actual == expected:
        return []
    errors: list[str] = []
    if actual.get("summary") != expected.get("summary"):
        errors.append("汇总指标与稳定基准不一致")
    expected_scenarios = {item["id"]: item for item in expected.get("scenarios", [])}
    actual_scenarios = {item["id"]: item for item in actual.get("scenarios", [])}
    for scenario_id in sorted(expected_scenarios.keys() | actual_scenarios.keys()):
        if actual_scenarios.get(scenario_id) != expected_scenarios.get(scenario_id):
            errors.append(f"场景基准漂移: {scenario_id}")
    return errors
