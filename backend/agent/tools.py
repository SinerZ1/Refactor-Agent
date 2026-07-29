import difflib
import hashlib
import os
import subprocess
from pathlib import Path
from typing import Annotated, Literal

from langchain_core.messages import ToolMessage
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.prebuilt import InjectedState
from langgraph.types import Command

from .state import ChangeRecord, State, TestRunRecord, compute_change_set_digest
from .workspace import resolve_workspace_path

# ============================================================
# 教学说明: 智能体工具库 (Agent Tooling)
# ------------------------------------------------------------
# 这里的工具在底层相当于 Hello-Agents 课程里讲过的 Tool 定义。
# 通过 @tool 装饰器，LangChain 能够自动根据函数签名和 Docstring
# 提取出 JSON Schema，并在 LLM 调用时序列化并传输，实现自动函数调用。
# ============================================================


PROJECT_ROOT = Path(__file__).resolve().parents[2]
REFACTOR_ROOT = (PROJECT_ROOT / "CodeSmells").resolve()
MAX_CODE_FILE_BYTES = 1_000_000
MAX_REVIEW_DIFF_CHARS = 12_000
MAX_TEST_OUTPUT_CHARS = 8_000


def get_project_root() -> str:
    """获取项目根目录，即 backend 的上一级目录"""
    return str(PROJECT_ROOT)


def resolve_path(file_path: str) -> str:
    """把模型给出的相对路径限制在 ``CodeSmells`` 重构工作区内。

    提示词只是软约束，路径规范化才是工具层的安全边界。先拒绝绝对路径，再解析
    ``..`` 与已有符号链接，确保最终目标仍位于允许根目录，避免 Agent 读取密钥、
    修改自身后端或越界访问用户文件。
    """

    if not file_path or not file_path.strip():
        raise ValueError("文件路径不能为空")
    requested_path = Path(file_path.strip())
    if requested_path.is_absolute():
        raise ValueError("仅允许使用 CodeSmells 目录内的相对路径")
    resolved_path = (PROJECT_ROOT / requested_path).resolve(strict=False)
    if not resolved_path.is_relative_to(REFACTOR_ROOT):
        raise ValueError("文件路径超出允许的 CodeSmells 重构工作区")
    return str(resolved_path)


@tool(response_format="content_and_artifact")
def read_code_file(
    file_path: str,
    state: Annotated[State, InjectedState],
) -> tuple[str, dict]:
    """
    读取指定路径下的本地代码文件内容。当需要查看某个具体文件的代码时使用。
    """
    try:
        workspace_id = state.get("workspace_id")
        if not workspace_id:
            raise ValueError("当前运行缺少隔离工作区")
        abs_path = str(resolve_workspace_path(workspace_id, file_path))
        if os.path.getsize(abs_path) > MAX_CODE_FILE_BYTES:
            return (
                "读取文件失败: 文件超过 1 MB 安全上限",
                {
                    "success": False,
                    "file_path": file_path,
                    "failure_kind": "size_limit",
                },
            )
        with open(abs_path, "r", encoding="utf-8") as f:
            return f.read(), {"success": True, "file_path": file_path}
    except Exception as e:
        return (
            f"读取文件失败: {e!s}",
            {"success": False, "file_path": file_path, "failure_kind": "read_error"},
        )


@tool
def write_code_file(
    file_path: str,
    content: str,
    state: Annotated[State, InjectedState],
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> Command:
    """
    将重构后的完整代码写入到指定的本地文件路径中。当重构完成并且需要保存修改时使用。
    """

    def tool_result(
        message: str,
        *,
        success: bool,
        failure_kind: str | None = None,
        change_record: ChangeRecord | None = None,
    ) -> Command:
        artifact = {
            "success": success,
            "file_path": file_path,
        }
        active_task_id = state.get("active_task_id")
        if active_task_id is not None:
            artifact["task_id"] = active_task_id
        if failure_kind is not None:
            artifact["failure_kind"] = failure_kind
        update: dict = {
            "messages": [
                ToolMessage(
                    content=message,
                    tool_call_id=tool_call_id,
                    name="write_code_file",
                    status="success" if success else "error",
                    artifact=artifact,
                )
            ]
        }
        if change_record is not None:
            update["change_records"] = [change_record]
        if active_task_id is not None:
            update["active_task_write_succeeded"] = success
            update["active_task_failure_reason"] = None if success else message
        return Command(update=update)

    try:
        if len(content.encode("utf-8")) > MAX_CODE_FILE_BYTES:
            return tool_result(
                "写入文件失败: 内容超过 1 MB 安全上限",
                success=False,
                failure_kind="size_limit",
            )
        workspace_id = state.get("workspace_id")
        if not workspace_id:
            return tool_result(
                "写入文件失败: 当前运行缺少隔离工作区",
                success=False,
                failure_kind="missing_workspace",
            )
        # 真实路径只用于校验任务授权；实际写入始终落到 run 的 working 快照。
        real_abs_path = resolve_path(file_path)
        abs_path = str(resolve_workspace_path(workspace_id, file_path))
        plan = state.get("refactor_plan")
        active_task_id = state.get("active_task_id")
        if plan is not None:
            active_task = next(
                (task for task in plan["tasks"] if task["id"] == active_task_id),
                None,
            )
            if active_task is None:
                return tool_result(
                    "写入文件失败: 动态计划不存在有效的当前活动任务",
                    success=False,
                    failure_kind="missing_active_task",
                )
            allowed_path = Path(resolve_path(active_task["file_path"])).resolve()
            if Path(real_abs_path).resolve() != allowed_path:
                return tool_result(
                    (
                        "写入文件失败: 当前任务 "
                        f"`{active_task_id}` 只允许写入 `{active_task['file_path']}`"
                    ),
                    success=False,
                    failure_kind="task_path_violation",
                )
        # 获取原文件代码（如果存在），供前端展示 Diff 对比
        original_code = ""
        if os.path.exists(abs_path):
            try:
                with open(abs_path, "r", encoding="utf-8") as f:
                    original_code = f.read()
            except Exception:
                pass

        # Developer 的 ToolResponse 只代表“隔离副本写入成功”，不再拥有真实源码提交
        # 权限。最终 Reviewer 通过后，聚合 diff 会在独立节点触发一次 HITL。
        dir_name = os.path.dirname(abs_path)
        if dir_name:
            os.makedirs(dir_name, exist_ok=True)
        with open(abs_path, "w", encoding="utf-8") as f:
            f.write(content)

        change_record = build_change_record(
            abs_path,
            original_code,
            content,
            relative_path_override=file_path,
        )
        return tool_result(
            f"成功将重构代码写入隔离工作区: {file_path}",
            success=True,
            change_record=change_record,
        )
    except Exception as e:
        return tool_result(
            f"写入文件失败: {e!s}",
            success=False,
            failure_kind="write_error",
        )


def build_change_record(
    absolute_path: str,
    original_code: str,
    refactored_code: str,
    *,
    relative_path_override: str | None = None,
) -> ChangeRecord:
    """构造 Reviewer 所需的结构化差异，不赋予其任意文件读取能力。

    这相当于把 ToolResponse 中的“写入成功”升级为可审计事件：哈希证明前后版本，
    unified diff 支持逻辑审查。diff 设置上限是持久化成本与审查完整度之间的权衡；
    截断会被显式标记，Reviewer 仍可据此要求 Developer 拆分修改。
    """

    relative_path = (
        Path(relative_path_override).as_posix()
        if relative_path_override is not None
        else Path(absolute_path).resolve().relative_to(PROJECT_ROOT).as_posix()
    )
    before_lines = original_code.splitlines()
    after_lines = refactored_code.splitlines()
    diff_lines = list(
        difflib.unified_diff(
            before_lines,
            after_lines,
            fromfile=f"a/{relative_path}",
            tofile=f"b/{relative_path}",
            lineterm="",
        )
    )
    added_lines = sum(
        line.startswith("+") and not line.startswith("+++") for line in diff_lines
    )
    removed_lines = sum(
        line.startswith("-") and not line.startswith("---") for line in diff_lines
    )
    full_diff = "\n".join(diff_lines)
    diff_truncated = len(full_diff) > MAX_REVIEW_DIFF_CHARS
    if diff_truncated:
        full_diff = (
            full_diff[:MAX_REVIEW_DIFF_CHARS]
            + "\n... [diff 已截断，请结合测试结果审查]"
        )

    return {
        "file_path": relative_path,
        "before_sha256": hashlib.sha256(original_code.encode("utf-8")).hexdigest(),
        "after_sha256": hashlib.sha256(refactored_code.encode("utf-8")).hexdigest(),
        "added_lines": added_lines,
        "removed_lines": removed_lines,
        "unified_diff": full_diff,
        "diff_truncated": diff_truncated,
    }


@tool
def run_unit_tests(
    state: Annotated[State, InjectedState],
    tool_call_id: Annotated[str, InjectedToolCallId],
    test_suite: Literal["backend", "codesmells", "all"] = "all",
) -> Command:
    """
    运行预定义测试套件。只能选择 backend、codesmells 或 all，不能传入 shell 命令。
    """
    suite_labels = {
        "backend": "backend/tests",
        "codesmells": "CodeSmells",
        "all": "backend/tests + CodeSmells",
    }
    change_set_digest = compute_change_set_digest(state.get("change_records", []))

    def bounded_output(output: str) -> str:
        """限制 ToolMessage 与 Checkpoint 日志体积，避免测试噪声放大状态存储。"""

        if len(output) <= MAX_TEST_OUTPUT_CHARS:
            return output
        truncation_marker = "\n... [测试输出已截断]"
        return (
            output[: MAX_TEST_OUTPUT_CHARS - len(truncation_marker)] + truncation_marker
        )

    def tool_result(
        *,
        output: str,
        success: bool,
        exit_code: int,
        failure_kind: str | None = None,
    ) -> Command:
        output_excerpt = bounded_output(output)
        record: TestRunRecord = {
            "suite": suite_labels[test_suite],
            "success": success,
            "exit_code": exit_code,
            "change_set_digest": change_set_digest,
            "output_excerpt": output_excerpt,
        }
        artifact: dict = {
            "success": success,
            "test_suite": test_suite,
            "suite": record["suite"],
            "exit_code": exit_code,
            "change_set_digest": change_set_digest,
        }
        if failure_kind is not None:
            artifact["failure_kind"] = failure_kind
        return Command(
            update={
                "messages": [
                    ToolMessage(
                        content=(
                            "测试执行完成。"
                            f"退出代码 (Exit Code): {exit_code}\n"
                            f"变更摘要: {change_set_digest}\n"
                            f"输出内容:\n{output_excerpt}"
                        ),
                        tool_call_id=tool_call_id,
                        name="run_unit_tests",
                        status="success" if success else "error",
                        artifact=artifact,
                    )
                ],
                "test_run_records": [record],
            }
        )

    try:
        encoding_format = "gbk" if os.name == "nt" else "utf-8"
        python_executable = PROJECT_ROOT / "backend" / "venv" / "Scripts" / "python.exe"
        workspace_id = state.get("workspace_id")
        if not workspace_id:
            raise ValueError("当前运行缺少隔离工作区")
        workspace_root = resolve_workspace_path(workspace_id, "CodeSmells").parent
        test_targets = {
            "backend": [str(PROJECT_ROOT / "backend" / "tests")],
            "codesmells": [str(workspace_root / "CodeSmells")],
            "all": [
                str(PROJECT_ROOT / "backend" / "tests"),
                str(workspace_root / "CodeSmells"),
            ],
        }
        command = [
            str(python_executable),
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            *test_targets[test_suite],
        ]
        test_environment = os.environ.copy()
        # Reviewer 测试属于隔离工作区的验证过程，字节码是运行副产物而非 Agent
        # 变更；禁止生成 pyc，避免它们进入最终哈希或聚合 diff。
        test_environment["PYTHONDONTWRITEBYTECODE"] = "1"
        test_environment["PYTHONPATH"] = os.pathsep.join(
            filter(
                None,
                [
                    str(workspace_root),
                    test_environment.get("PYTHONPATH", ""),
                ],
            )
        )
        result = subprocess.run(
            command,
            shell=False,
            capture_output=True,
            text=True,
            encoding=encoding_format,
            errors="replace",
            timeout=60,
            cwd=str(workspace_root),
            env=test_environment,
            check=False,
        )
        output = (result.stdout or "").strip() + "\n" + (result.stderr or "").strip()
        output = output.strip()
        if not output:
            output = "<无任何标准输出或错误输出 (No output)>"
        success = result.returncode == 0
        return tool_result(
            output=output,
            success=success,
            exit_code=result.returncode,
            failure_kind=None if success else "test_failure",
        )
    except Exception as e:
        return tool_result(
            output=f"运行测试失败: {e!s}",
            success=False,
            exit_code=-1,
            failure_kind="test_execution_error",
        )


@tool(response_format="content_and_artifact")
def search_symbol_definition(symbol_name: str) -> tuple[str, dict]:
    """
    当你分析或重构当前文件，遇到外部导入的类名、函数名时，可以使用此工具查询它在本项目其他文件中的原始定义和源代码，支持精准跨文件上下文召回（RAG）。
    """
    # 动态导入避免循环依赖
    from code_indexer import get_symbol_definition_content

    return get_symbol_definition_content(symbol_name), {
        "success": True,
        "symbol_name": symbol_name,
    }


@tool(response_format="content_and_artifact")
def query_neo4j_topology() -> tuple[str, dict]:
    """
    查询 Neo4j 数据库中的项目代码调用图谱。返回项目中所有类、函数（节点）以及它们之间的调用关系（CALLS 边）。
    这能帮助你快速理清跨文件的代码依赖、调用拓扑和项目结构。
    """
    # 动态导入避免循环依赖
    from graph_indexer import get_topology_data

    try:
        data = get_topology_data()
        fallback_str = " (AST 降级内存图模式)" if data.get("fallback") else ""
        result_str = f"=== 项目代码调用图谱{fallback_str} ===\n"
        result_str += "【节点 (Symbols)】:\n"
        for node in data["nodes"]:
            result_str += (
                f"- [{node['type']}] {node['name']} (定义于 {node['file_path']})\n"
            )
        result_str += "\n【调用关系 (CALLS Relationships)】:\n"
        for link in data["links"]:
            result_str += f"- {link['source']} -> {link['target']}\n"
        result_str += "================================="
        return result_str, {
            "success": True,
            "degraded": bool(data.get("fallback")),
        }
    except Exception as e:
        return (
            f"查询 Neo4j 拓扑图谱失败: {e!s}",
            {"success": False, "failure_kind": "topology_query_error"},
        )


# 区分不同智能体的工具集合
architect_tools = [read_code_file, search_symbol_definition, query_neo4j_topology]
developer_tools = [read_code_file, write_code_file, search_symbol_definition]
reviewer_tools = [run_unit_tests]
