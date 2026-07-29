import json
from pathlib import Path
from typing import Any


def _percent(value: float) -> str:
    return f"{value * 100:.2f}%"


def render_markdown(report: dict[str, Any]) -> str:
    summary = report["summary"]
    tokens = summary["token_usage"]
    lines = [
        "# Refactor-Agent 评测报告",
        "",
        f"- 模式：`{report['mode']}`",
        f"- 模型：`{report['model']}`",
        f"- 场景数：{summary['scenario_count']}",
        f"- 行为测试通过率：{_percent(summary['behavior_test_pass_rate'])}",
        f"- 首次审查成功率：{_percent(summary['first_review_success_rate'])}",
        f"- 最终成功率：{_percent(summary['final_success_rate'])}",
        f"- 平均重试次数：{summary['average_retry_count']:.4f}",
        f"- 工具调用次数：{summary['tool_call_count']}",
        (
            "- Token 使用量："
            f"{tokens['total_tokens']}（输入 {tokens['input_tokens']} / "
            f"输出 {tokens['output_tokens']}）"
        ),
        f"- 总耗时：{summary['duration_seconds']:.6f} 秒",
        f"- HITL 拒绝率：{_percent(summary['hitl_rejection_rate'])}",
        f"- 安全策略拦截率：{_percent(summary['safety_policy_block_rate'])}",
        f"- 模型错误数：{summary['model_error_count']}",
        "",
        "## 场景结果",
        "",
        "| 场景 | 类别 | 行为检查 | 首审 | 终态 | 重试 | 工具 | Token |",
        "| --- | --- | --- | --- | --- | ---: | ---: | ---: |",
    ]
    for result in report["scenarios"]:
        metrics = result["metrics"]
        first_review = metrics["first_review_success"]
        first_review_text = (
            "不适用" if first_review is None else ("通过" if first_review else "未通过")
        )
        lines.append(
            "| "
            f"`{result['id']}` | {result['category']} | "
            f"{'通过' if result['passed'] else '失败'} | {first_review_text} | "
            f"`{result['outcome']['terminal_status']}` | "
            f"{metrics['retry_count']} | {metrics['tool_calls']} | "
            f"{metrics['total_tokens']} |"
        )
    lines.extend(
        [
            "",
            "## 数据边界",
            "",
            "报告只包含归一化指标和判定，不保存 Prompt、源码全文、供应商原始响应、"
            "API Key、数据库口令或凭据路径。离线模式是控制面回归基准，不能替代"
            "真实模型质量评测。",
            "",
        ]
    )
    return "\n".join(lines)


def write_reports(report: dict[str, Any], output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "report.json"
    markdown_path = output_dir / "report.md"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(render_markdown(report), encoding="utf-8")
    return json_path, markdown_path
