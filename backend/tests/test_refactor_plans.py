import pytest

from agent.plans import find_plan_task_id, parse_refactor_plan, validate_refactor_plan


def _plan(tasks):
    return {
        "version": 1,
        "summary": "拆分数据层和入口编排",
        "tasks": tasks,
    }


def _task(task_id, file_path, dependencies=None):
    return {
        "id": task_id,
        "title": f"处理 {task_id}",
        "description": "完成单一文件的重构并保持行为兼容",
        "file_path": file_path,
        "dependencies": dependencies or [],
    }


def test_plan_parser_normalizes_paths_and_preserves_dependencies():
    message = """
先拆分模型，再调整入口。
```refactor_plan
{
  "version": 1,
  "summary": "分层重构",
  "tasks": [
    {
      "id": "models",
      "title": "整理数据模型",
      "description": "提取数据对象",
      "file_path": "CodeSmells\\\\models.py",
      "dependencies": []
    },
    {
      "id": "entrypoint",
      "title": "调整入口",
      "description": "组合新的数据对象",
      "file_path": "CodeSmells/main.py",
      "dependencies": ["models"]
    }
  ]
}
```
"""

    plan, error = parse_refactor_plan(message)

    assert error is None
    assert plan is not None
    assert plan["tasks"][0]["file_path"] == "CodeSmells/models.py"
    assert plan["tasks"][1]["dependencies"] == ["models"]
    assert find_plan_task_id(plan, "CodeSmells\\main.py") == "entrypoint"


@pytest.mark.parametrize(
    ("tasks", "expected_message"),
    [
        (
            [
                _task("first", "CodeSmells/first.py", ["second"]),
                _task("second", "CodeSmells/second.py", ["first"]),
            ],
            "存在环",
        ),
        (
            [_task("first", "CodeSmells/first.py", ["missing"])],
            "不存在的依赖",
        ),
        (
            [
                _task("first", "CodeSmells/shared.py"),
                _task("second", "CodeSmells/shared.py"),
            ],
            "同一文件",
        ),
        (
            [_task("first", "../backend/app.py")],
            "超出允许",
        ),
        (
            [_task("first", "CodeSmells/settings.json")],
            "Python 文件",
        ),
    ],
)
def test_plan_validator_rejects_unsafe_or_unschedulable_graphs(tasks, expected_message):
    with pytest.raises(ValueError, match=expected_message):
        validate_refactor_plan(_plan(tasks))


def test_missing_plan_block_is_a_nonfatal_fallback():
    plan, error = parse_refactor_plan("只有自然语言设计方案")

    assert plan is None
    assert error is not None
    assert "默认任务图" in error
