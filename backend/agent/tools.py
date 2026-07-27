import difflib
import hashlib
import os
import subprocess
from pathlib import Path
from typing import Annotated, Literal

from langchain_core.messages import ToolMessage
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.errors import GraphInterrupt
from langgraph.types import Command, interrupt

from .state import ChangeRecord

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
def read_code_file(file_path: str) -> tuple[str, dict]:
    """
    读取指定路径下的本地代码文件内容。当需要查看某个具体文件的代码时使用。
    """
    try:
        abs_path = resolve_path(file_path)
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
        return Command(update=update)

    try:
        if len(content.encode("utf-8")) > MAX_CODE_FILE_BYTES:
            return tool_result(
                "写入文件失败: 内容超过 1 MB 安全上限",
                success=False,
                failure_kind="size_limit",
            )
        abs_path = resolve_path(file_path)
        # 获取原文件代码（如果存在），供前端展示 Diff 对比
        original_code = ""
        if os.path.exists(abs_path):
            try:
                with open(abs_path, "r", encoding="utf-8") as f:
                    original_code = f.read()
            except Exception:
                pass

        # 暂停状态机执行，向前端返回审批数据包。
        # 这里在底层相当于 Hello-Agents 课程中工具暂停返回人机协作决策状态。
        # 状态机此时会在 Checkpointer 中挂起并保存现场，恢复（resume）后，它将返回用户反馈的数据包。
        approval_res = interrupt(
            {
                "type": "write_approval",
                "file_path": abs_path,
                "original_code": original_code,
                "refactored_code": content,
            }
        )

        # 提取并验证前端返回的审批结果
        approved = False
        if isinstance(approval_res, bool):
            approved = approval_res
        elif isinstance(approval_res, dict):
            approved = approval_res.get("approved", False)

        if not approved:
            return tool_result(
                f"写入文件 `{abs_path}` 失败：用户在人机协作审批中点击拒绝，打回修改。",
                success=False,
                failure_kind="approval_rejected",
            )

        # 审批通过，执行本地写入
        dir_name = os.path.dirname(abs_path)
        if dir_name:
            os.makedirs(dir_name, exist_ok=True)
        with open(abs_path, "w", encoding="utf-8") as f:
            f.write(content)

        # 写文件与索引更新属于同一业务事实：若只更新磁盘，后续 Agent 会从 RAG
        # 读取旧快照，形成“已修改但仍按旧代码推理”的时间一致性缺陷。
        index_message = ""
        try:
            from code_indexer import index_file
            from graph_indexer import index_to_neo4j

            symbol_count = index_file(abs_path)
            neo4j_updated = index_to_neo4j()
            index_message = (
                f"；AST 索引已刷新 {symbol_count} 个符号，"
                f"Neo4j {'已同步' if neo4j_updated else '不可用，已保留 AST 降级模式'}"
            )
        except Exception as index_error:
            index_message = f"；索引刷新失败，请重新构建索引: {index_error}"
        change_record = build_change_record(abs_path, original_code, content)
        return tool_result(
            f"成功将重构代码写入到文件: {abs_path}{index_message}",
            success=True,
            change_record=change_record,
        )
    except GraphInterrupt:
        # 重要：必须重新抛出 GraphInterrupt，否则会被底下的 Exception 捕获
        # 从而导致 LangGraph 的中断挂起机制失效，直接把打断异常当作普通错误返回给大模型
        raise
    except Exception as e:
        return tool_result(
            f"写入文件失败: {e!s}",
            success=False,
            failure_kind="write_error",
        )


def build_change_record(
    absolute_path: str, original_code: str, refactored_code: str
) -> ChangeRecord:
    """构造 Reviewer 所需的结构化差异，不赋予其任意文件读取能力。

    这相当于把 ToolResponse 中的“写入成功”升级为可审计事件：哈希证明前后版本，
    unified diff 支持逻辑审查。diff 设置上限是持久化成本与审查完整度之间的权衡；
    截断会被显式标记，Reviewer 仍可据此要求 Developer 拆分修改。
    """

    relative_path = Path(absolute_path).resolve().relative_to(PROJECT_ROOT).as_posix()
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


@tool(response_format="content_and_artifact")
def run_unit_tests(
    test_suite: Literal["backend", "codesmells", "all"] = "all",
) -> tuple[str, dict]:
    """
    运行预定义测试套件。只能选择 backend、codesmells 或 all，不能传入 shell 命令。
    """
    try:
        encoding_format = "gbk" if os.name == "nt" else "utf-8"
        python_executable = PROJECT_ROOT / "backend" / "venv" / "Scripts" / "python.exe"
        test_targets = {
            "backend": ["backend/tests"],
            "codesmells": ["CodeSmells"],
            "all": ["backend/tests", "CodeSmells"],
        }
        command = [
            str(python_executable),
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            *test_targets[test_suite],
        ]
        result = subprocess.run(
            command,
            shell=False,
            capture_output=True,
            text=True,
            encoding=encoding_format,
            errors="replace",
            timeout=60,
            cwd=get_project_root(),
            check=False,
        )
        output = (result.stdout or "").strip() + "\n" + (result.stderr or "").strip()
        output = output.strip()
        if not output:
            output = "<无任何标准输出或错误输出 (No output)>"
        success = result.returncode == 0
        return (
            f"测试执行完成。退出代码 (Exit Code): {result.returncode}\n输出内容:\n{output}",
            {
                "success": success,
                "test_suite": test_suite,
                "exit_code": result.returncode,
                **({} if success else {"failure_kind": "test_failure"}),
            },
        )
    except Exception as e:
        return (
            f"运行测试失败: {e!s}",
            {
                "success": False,
                "test_suite": test_suite,
                "failure_kind": "test_execution_error",
            },
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
