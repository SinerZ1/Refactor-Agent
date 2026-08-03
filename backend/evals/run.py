import argparse
import json
import sys
from pathlib import Path

from .models import EvaluationModel, LiveEvaluationModel
from .reporting import write_reports
from .runner import EvaluationRunner, baseline_drift
from .scenarios import SCENARIOS, scenario_by_id

EVALS_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT_DIR = EVALS_DIR / "output"
DEFAULT_BASELINE = EVALS_DIR / "baselines" / "offline.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="运行 Refactor-Agent 离线或真实模型评测。"
    )
    parser.add_argument(
        "--mode",
        choices=("offline", "live"),
        default="offline",
        help="offline 不调用 API；live 会显式调用真实模型并可能产生费用。",
    )
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="显式审核后更新完整离线稳定基准；不能与单场景或 live 模式合用。",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="JSON 和 Markdown 报告目录。",
    )
    parser.add_argument(
        "--scenario",
        action="append",
        default=[],
        help="只运行指定场景 ID；可重复传入。",
    )
    parser.add_argument(
        "--provider", choices=("openai", "gemini_studio", "google_vertex")
    )
    parser.add_argument("--model")
    parser.add_argument("--base-url")
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument(
        "--skip-baseline-check",
        action="store_true",
        help="离线模式不检查已提交的稳定基准。",
    )
    return parser


def _selected_scenarios(ids: list[str]):
    if not ids:
        return SCENARIOS
    try:
        return tuple(scenario_by_id(scenario_id) for scenario_id in ids)
    except KeyError as error:
        raise SystemExit(str(error)) from error


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.update_baseline and (args.mode != "offline" or args.scenario):
        raise SystemExit("--update-baseline 只允许用于完整 offline 评测")
    scenarios = _selected_scenarios(args.scenario)
    model: EvaluationModel | None
    if args.mode == "offline":
        model = None
    else:
        if not args.provider or not args.model:
            raise SystemExit("live 模式必须显式提供 --provider 和 --model")
        model = LiveEvaluationModel(
            provider=args.provider,
            model_name=args.model,
            base_url=args.base_url,
            temperature=args.temperature,
        )

    report = EvaluationRunner(
        model=model,
        mode=args.mode,
        scenarios=scenarios,
    ).run()
    json_path, markdown_path = write_reports(report, args.output_dir)
    summary = report["summary"]
    print(
        "评测完成："
        f"{summary['scenario_pass_count']}/{summary['scenario_count']} 场景通过，"
        f"行为测试通过率 {summary['behavior_test_pass_rate'] * 100:.2f}%"
    )
    print(f"JSON 报告: {json_path}")
    print(f"Markdown 报告: {markdown_path}")

    if args.mode == "offline" and not args.scenario and not args.skip_baseline_check:
        if args.update_baseline:
            from .runner import stable_baseline

            DEFAULT_BASELINE.write_text(
                json.dumps(stable_baseline(report), ensure_ascii=False, indent=2)
                + "\n",
                encoding="utf-8",
            )
            print(f"离线稳定基准已显式更新: {DEFAULT_BASELINE}")
            return 0 if summary["model_error_count"] == 0 else 1
        drift = baseline_drift(report, DEFAULT_BASELINE)
        if drift:
            print("离线基准检查失败：")
            for error in drift:
                print(f"- {error}")
            return 1
        print("离线稳定基准检查通过。")
    return 0 if summary["model_error_count"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
