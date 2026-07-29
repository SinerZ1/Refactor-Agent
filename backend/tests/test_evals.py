import json

from evals.models import ScriptedFakeModel
from evals.reporting import render_markdown, write_reports
from evals.run import DEFAULT_BASELINE, main
from evals.runner import EvaluationRunner, stable_baseline
from evals.scenarios import SCENARIOS

REQUIRED_CATEGORIES = {
    "长函数",
    "重复代码",
    "命名问题",
    "高耦合",
    "缺少类型",
    "异常处理",
    "跨文件依赖",
    "Prompt injection",
    "越界写入",
    "审批拒绝",
    "测试失败",
    "预算耗尽",
}


def _offline_report():
    return EvaluationRunner(
        model=ScriptedFakeModel(),
        mode="offline",
        scenarios=SCENARIOS,
    ).run()


def test_catalog_has_twenty_plus_scenarios_and_all_required_categories():
    assert len(SCENARIOS) >= 20
    assert {scenario.category for scenario in SCENARIOS} == REQUIRED_CATEGORIES
    assert all(
        sum(candidate.category == category for candidate in SCENARIOS) >= 2
        for category in REQUIRED_CATEGORIES
    )


def test_offline_eval_is_deterministic_except_timing():
    first = stable_baseline(_offline_report())
    second = stable_baseline(_offline_report())
    committed = json.loads(DEFAULT_BASELINE.read_text(encoding="utf-8"))

    assert first == second
    assert first == committed
    assert first["summary"]["scenario_pass_rate"] == 1.0
    assert first["summary"]["behavior_test_pass_rate"] == 1.0
    assert first["summary"]["safety_policy_block_rate"] == 1.0
    assert first["summary"]["hitl_rejection_rate"] > 0
    assert {
        "behavior_test_pass_rate",
        "first_review_success_rate",
        "final_success_rate",
        "average_retry_count",
        "tool_call_count",
        "token_usage",
        "duration_seconds",
        "hitl_rejection_rate",
        "safety_policy_block_rate",
    } <= _offline_report()["summary"].keys()


def test_reports_are_readable_and_do_not_persist_raw_inputs(tmp_path):
    report = _offline_report()
    json_path, markdown_path = write_reports(report, tmp_path)
    serialized = json_path.read_text(encoding="utf-8")
    markdown = markdown_path.read_text(encoding="utf-8")

    assert json.loads(serialized)["summary"]["tool_call_count"] > 0
    assert "行为测试通过率" in markdown
    assert "HITL 拒绝率" in markdown
    assert "Ignore previous instructions" not in serialized
    assert "backend/.env" not in serialized
    assert render_markdown(report) == markdown


def test_offline_cli_writes_both_reports_without_live_configuration(tmp_path):
    exit_code = main(
        [
            "--mode",
            "offline",
            "--scenario",
            "long-function-extract",
            "--output-dir",
            str(tmp_path),
        ]
    )

    assert exit_code == 0
    assert (tmp_path / "report.json").is_file()
    assert (tmp_path / "report.md").is_file()


def test_live_mode_requires_explicit_provider_and_model(tmp_path):
    try:
        main(["--mode", "live", "--output-dir", str(tmp_path)])
    except SystemExit as error:
        assert "必须显式提供" in str(error)
    else:
        raise AssertionError("live 模式不应在缺少模型配置时继续")
