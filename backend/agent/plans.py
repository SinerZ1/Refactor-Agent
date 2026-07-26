import json
import re
from pathlib import Path
from typing import Any, Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

PROJECT_ROOT = Path(__file__).resolve().parents[2]
REFACTOR_ROOT = (PROJECT_ROOT / "CodeSmells").resolve()
PLAN_BLOCK_PATTERN = re.compile(
    r"```refactor_plan\s*(\{.*?\})\s*```",
    flags=re.DOTALL,
)
TASK_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
RESERVED_TASK_IDS = {"architect_task", "reviewer_task"}
MAX_PLAN_TASKS = 20


class RefactorTask(TypedDict):
    id: str
    title: str
    description: str
    file_path: str
    dependencies: list[str]


class RefactorPlan(TypedDict):
    version: Literal[1]
    summary: str
    tasks: list[RefactorTask]


class _RefactorTaskInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=1000)
    file_path: str = Field(min_length=1, max_length=500)
    dependencies: list[str] = Field(default_factory=list, max_length=MAX_PLAN_TASKS)

    @field_validator("id")
    @classmethod
    def validate_task_id(cls, value: str) -> str:
        if not TASK_ID_PATTERN.fullmatch(value):
            raise ValueError("任务 ID 只能使用小写字母、数字、下划线和连字符")
        if value in RESERVED_TASK_IDS:
            raise ValueError("任务 ID 与系统节点冲突")
        return value


class _RefactorPlanInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    version: Literal[1]
    summary: str = Field(min_length=1, max_length=1000)
    tasks: list[_RefactorTaskInput] = Field(
        min_length=1,
        max_length=MAX_PLAN_TASKS,
    )


def canonicalize_refactor_path(file_path: str) -> str:
    """把计划路径转换为唯一的仓库相对路径，并复用工具层同等级别的安全边界。

    Prompt 约束属于概率性的“软控制”，真实路径解析才是确定性的“硬控制”。这里先
    拒绝绝对路径，再解析 ``..`` 与符号链接；这相当于在 LLM 规划层和执行工具层之间
    建立 capability boundary，防止一个看似合法的 DAG 把后续写入能力引向工作区外。
    """

    if not file_path or not file_path.strip():
        raise ValueError("文件路径不能为空")
    requested_path = Path(file_path.strip())
    if requested_path.is_absolute():
        raise ValueError("计划只能引用 CodeSmells 目录内的相对路径")
    resolved_path = (PROJECT_ROOT / requested_path).resolve(strict=False)
    if not resolved_path.is_relative_to(REFACTOR_ROOT):
        raise ValueError("计划路径超出允许的 CodeSmells 重构工作区")
    if resolved_path.suffix.casefold() != ".py":
        raise ValueError("计划任务只能引用 CodeSmells 目录内的 Python 文件")
    return resolved_path.relative_to(PROJECT_ROOT).as_posix()


def _assert_acyclic(tasks: list[RefactorTask]) -> None:
    """使用 Kahn 拓扑排序验证 DAG，而不是信任模型声称“依赖无环”。

    入度归零过程同时验证所有任务都能被调度。若消费数量小于任务总数，剩余节点必然
    位于环中；在进入 LangGraph 前拒绝该计划，可避免 UI 与未来任务调度器永久等待。
    """

    indegree = {task["id"]: len(task["dependencies"]) for task in tasks}
    dependents: dict[str, list[str]] = {task["id"]: [] for task in tasks}
    for task in tasks:
        for dependency in task["dependencies"]:
            dependents[dependency].append(task["id"])

    ready = [task_id for task_id, degree in indegree.items() if degree == 0]
    visited = 0
    while ready:
        task_id = ready.pop()
        visited += 1
        for dependent in dependents[task_id]:
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                ready.append(dependent)

    if visited != len(tasks):
        raise ValueError("任务依赖中存在环，无法构成 DAG")


def validate_refactor_plan(value: Any) -> RefactorPlan:
    """把不可信的模型 JSON 归一化为可进入 Checkpoint 与事件流的计划事实。"""

    parsed = _RefactorPlanInput.model_validate(value)
    task_ids = [task.id for task in parsed.tasks]
    if len(task_ids) != len(set(task_ids)):
        raise ValueError("任务 ID 必须唯一")

    known_ids = set(task_ids)
    normalized_tasks: list[RefactorTask] = []
    seen_paths: set[str] = set()
    for task in parsed.tasks:
        if len(task.dependencies) != len(set(task.dependencies)):
            raise ValueError(f"任务 {task.id} 包含重复依赖")
        if task.id in task.dependencies:
            raise ValueError(f"任务 {task.id} 不能依赖自身")
        unknown_dependencies = set(task.dependencies) - known_ids
        if unknown_dependencies:
            unknown = ", ".join(sorted(unknown_dependencies))
            raise ValueError(f"任务 {task.id} 引用了不存在的依赖: {unknown}")

        normalized_path = canonicalize_refactor_path(task.file_path)
        path_identity = normalized_path.casefold()
        if path_identity in seen_paths:
            raise ValueError(f"多个任务不能写入同一文件: {normalized_path}")
        seen_paths.add(path_identity)
        normalized_tasks.append(
            {
                "id": task.id,
                "title": task.title,
                "description": task.description,
                "file_path": normalized_path,
                "dependencies": list(task.dependencies),
            }
        )

    _assert_acyclic(normalized_tasks)
    return {
        "version": 1,
        "summary": parsed.summary,
        "tasks": normalized_tasks,
    }


def parse_refactor_plan(message_text: str) -> tuple[RefactorPlan | None, str | None]:
    """提取 Architect 的最终计划；失败返回原因，让调用方选择兼容降级而非崩溃。"""

    matches = list(PLAN_BLOCK_PATTERN.finditer(message_text))
    if not matches:
        return None, "Architect 未输出 refactor_plan 结构化计划块，已使用默认任务图"
    if len(matches) > 1:
        return None, "Architect 输出了多个 refactor_plan 计划块，已使用默认任务图"
    try:
        raw_plan = json.loads(matches[0].group(1))
        return validate_refactor_plan(raw_plan), None
    except (json.JSONDecodeError, ValidationError, ValueError) as error:
        return None, f"Architect 结构化计划校验失败，已使用默认任务图: {error}"


def find_plan_task_id(
    plan: RefactorPlan | None,
    file_path: object,
) -> str | None:
    """用规范路径把工具事件关联回计划任务，避免依赖文件名或日志文本猜测。"""

    if plan is None or not isinstance(file_path, str):
        return None
    try:
        normalized_path = canonicalize_refactor_path(file_path)
    except ValueError:
        return None
    for task in plan["tasks"]:
        if task["file_path"].casefold() == normalized_path.casefold():
            return task["id"]
    return None
