import os
import subprocess
from pathlib import Path
from typing import Literal

from langchain_core.tools import tool
from langgraph.errors import GraphInterrupt
from langgraph.types import interrupt

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


@tool
def read_code_file(file_path: str) -> str:
    """
    读取指定路径下的本地代码文件内容。当需要查看某个具体文件的代码时使用。
    """
    try:
        abs_path = resolve_path(file_path)
        if os.path.getsize(abs_path) > MAX_CODE_FILE_BYTES:
            return "读取文件失败: 文件超过 1 MB 安全上限"
        with open(abs_path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        return f"读取文件失败: {str(e)}"


@tool
def write_code_file(file_path: str, content: str) -> str:
    """
    将重构后的完整代码写入到指定的本地文件路径中。当重构完成并且需要保存修改时使用。
    """
    try:
        if len(content.encode("utf-8")) > MAX_CODE_FILE_BYTES:
            return "写入文件失败: 内容超过 1 MB 安全上限"
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
            return (
                f"写入文件 `{abs_path}` 失败：用户在人机协作审批中点击拒绝，打回修改。"
            )

        # 审批通过，执行本地写入
        dir_name = os.path.dirname(abs_path)
        if dir_name:
            os.makedirs(dir_name, exist_ok=True)
        with open(abs_path, "w", encoding="utf-8") as f:
            f.write(content)
        return f"成功将重构代码写入到文件: {abs_path}"
    except GraphInterrupt:
        # 重要：必须重新抛出 GraphInterrupt，否则会被底下的 Exception 捕获
        # 从而导致 LangGraph 的中断挂起机制失效，直接把打断异常当作普通错误返回给大模型
        raise
    except Exception as e:
        return f"写入文件失败: {str(e)}"


@tool
def run_unit_tests(
    test_suite: Literal["backend", "codesmells", "all"] = "all",
) -> str:
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
        )
        output = (result.stdout or "").strip() + "\n" + (result.stderr or "").strip()
        output = output.strip()
        if not output:
            output = "<无任何标准输出或错误输出 (No output)>"
        return f"测试执行完成。退出代码 (Exit Code): {result.returncode}\n输出内容:\n{output}"
    except Exception as e:
        return f"运行测试失败: {str(e)}"


@tool
def search_symbol_definition(symbol_name: str) -> str:
    """
    当你分析或重构当前文件，遇到外部导入的类名、函数名时，可以使用此工具查询它在本项目其他文件中的原始定义和源代码，支持精准跨文件上下文召回（RAG）。
    """
    # 动态导入避免循环依赖
    from code_indexer import get_symbol_definition_content

    return get_symbol_definition_content(symbol_name)


@tool
def query_neo4j_topology() -> str:
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
        return result_str
    except Exception as e:
        return f"查询 Neo4j 拓扑图谱失败: {str(e)}"


# 区分不同智能体的工具集合
architect_tools = [read_code_file, search_symbol_definition, query_neo4j_topology]
developer_tools = [read_code_file, write_code_file, search_symbol_definition]
reviewer_tools = [run_unit_tests]
