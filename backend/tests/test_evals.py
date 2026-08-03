import json
import socket
from dataclasses import replace

import pytest

from evals.control_plane import run_control_plane_scenario
from evals.models import ScriptedFakeModel
from evals.reporting import render_markdown, write_reports
from evals.run import DEFAULT_BASELINE, main
from evals.runner import (
    EvaluationRunner,
    baseline_drift,
    score_outcome,
    stable_baseline,
)
from evals.scenarios import SCENARIOS, scenario_by_id, with_expected

REQUIRED_CONTROL_PLANE_SCENARIOS = {
    "single-task-success",
    "serial-dag-success",
    "invalid-architect-plan",
    "developer-unauthorized-write",
    "protected-test-write",
    "reviewer-without-tests",
    "stale-test-evidence",
    "test-failure-retry",
    "review-reopens-downstream",
    "review-retry-exhausted",
    "upstream-failure-blocks-downstream",
    "token-budget-circuit-breaker",
    "hitl-approved",
    "hitl-rejected",
    "working-summary-tampered",
    "baseline-conflict",
    "dangerous-parent-path",
    "infrastructure-offline-fallback",
    "cleanup-after-terminal-failure",
    "state-isolation-first",
    "state-isolation-second",
}


def _offline_report(scenarios=SCENARIOS):
    return EvaluationRunner(model=None, mode="offline", scenarios=scenarios).run()


def test_catalog_covers_required_production_control_plane_paths():
    assert len(SCENARIOS) >= 20
    assert REQUIRED_CONTROL_PLANE_SCENARIOS <= {scenario.id for scenario in SCENARIOS}
    assert all(scenario.script for scenario in SCENARIOS)
    assert all(scenario.input.source_files for scenario in SCENARIOS)


def test_offline_eval_is_deterministic_and_matches_committed_baseline():
    first = stable_baseline(_offline_report())
    second = stable_baseline(_offline_report())
    committed = json.loads(DEFAULT_BASELINE.read_text(encoding="utf-8"))

    assert first == second == committed
    assert first["summary"]["scenario_pass_rate"] == 1.0
    assert first["summary"]["behavior_test_pass_rate"] == 1.0


def test_expected_is_only_an_oracle_and_cannot_change_actual_trace():
    scenario = scenario_by_id("single-task-success")
    actual = run_control_plane_scenario(scenario)
    wrong_expected = replace(scenario.expected, terminal_status="test_failure")
    altered = with_expected(scenario, wrong_expected)

    assert run_control_plane_scenario(altered) == actual
    assert score_outcome(scenario, actual)["terminal_status"] is True
    assert score_outcome(altered, actual)["terminal_status"] is False


def test_fake_model_constructor_accepts_script_not_scenario_or_expected():
    scenario = scenario_by_id("single-task-success")
    model = ScriptedFakeModel(scenario.script)

    assert not hasattr(model, "expected")
    assert not hasattr(model, "scenario")


def test_undeclared_model_call_fails_closed():
    scenario = scenario_by_id("single-task-success")
    truncated = replace(scenario, script=scenario.script[:1])

    with pytest.raises(AssertionError):
        run_control_plane_scenario(truncated)


def test_unconsumed_script_fails_closed():
    scenario = scenario_by_id("single-task-success")
    extended = replace(scenario, script=(*scenario.script, scenario.script[-1]))

    with pytest.raises(AssertionError, match="脚本未消费完"):
        run_control_plane_scenario(extended)


def test_control_plane_route_change_changes_actual_not_oracle():
    safe = scenario_by_id("single-task-success")
    attack = scenario_by_id("developer-unauthorized-write")

    safe_actual = run_control_plane_scenario(safe)
    attack_actual = run_control_plane_scenario(attack)
    assert safe_actual.terminal_status == "success"
    assert attack_actual.terminal_status == "safety_blocked"
    assert "tool.failed" in attack_actual.event_types


def test_baseline_drift_is_nonzero_and_default_command_does_not_modify_it(tmp_path):
    baseline_copy = tmp_path / "offline.json"
    baseline_copy.write_text("{}\n", encoding="utf-8")
    report = _offline_report((scenario_by_id("single-task-success"),))
    before = DEFAULT_BASELINE.read_bytes()

    assert baseline_drift(report, baseline_copy)
    assert (
        main(
            [
                "--mode",
                "offline",
                "--scenario",
                "single-task-success",
                "--output-dir",
                str(tmp_path / "output"),
            ]
        )
        == 0
    )
    assert DEFAULT_BASELINE.read_bytes() == before


def test_explicit_baseline_update_requires_full_offline_run(tmp_path, monkeypatch):
    baseline = tmp_path / "offline.json"
    monkeypatch.setattr("evals.run.DEFAULT_BASELINE", baseline)

    assert (
        main(
            [
                "--mode",
                "offline",
                "--update-baseline",
                "--output-dir",
                str(tmp_path / "output"),
            ]
        )
        == 0
    )
    assert json.loads(baseline.read_text(encoding="utf-8"))["summary"][
        "scenario_pass_count"
    ] == len(SCENARIOS)
    with pytest.raises(SystemExit, match="只允许用于完整 offline"):
        main(
            [
                "--mode",
                "offline",
                "--update-baseline",
                "--scenario",
                "single-task-success",
            ]
        )


def test_offline_control_plane_does_not_open_network_socket(monkeypatch):
    def reject_network(*_args, **_kwargs):
        raise AssertionError("离线 Eval 不得发起网络请求")

    monkeypatch.setattr(socket, "create_connection", reject_network)
    actual = run_control_plane_scenario(
        scenario_by_id("infrastructure-offline-fallback")
    )
    assert actual.terminal_status == "success"
    assert actual.workspace_cleaned is True


def test_reports_are_safe_and_do_not_persist_inputs_or_raw_responses(tmp_path):
    report = _offline_report((scenario_by_id("dangerous-parent-path"),))
    json_path, markdown_path = write_reports(report, tmp_path)
    serialized = json_path.read_text(encoding="utf-8")
    markdown = markdown_path.read_text(encoding="utf-8")

    assert "../backend/app.py" not in serialized
    assert "source_files" not in serialized
    assert '"script":' not in serialized
    assert "Prompt" not in serialized
    assert "API Key" not in serialized
    assert render_markdown(report) == markdown


def test_live_mode_requires_explicit_provider_and_model(tmp_path):
    with pytest.raises(SystemExit, match="必须显式提供"):
        main(["--mode", "live", "--output-dir", str(tmp_path)])
