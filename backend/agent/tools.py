import os
import subprocess

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


def get_project_root() -> str:
    """获取项目根目录，即 backend 的上一级目录"""
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "../../"))


def resolve_path(file_path: str) -> str:
    """将相对路径解析为基于项目根目录的绝对路径"""
    if os.path.isabs(file_path):
        return file_path
    return os.path.join(get_project_root(), file_path)


@tool
def read_code_file(file_path: str) -> str:
    """
    读取指定路径下的本地代码文件内容。当需要查看某个具体文件的代码时使用。
    """
    try:
        abs_path = resolve_path(file_path)
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
def run_unit_tests(test_command: str = "pytest") -> str:
    """
    执行本项目的测试命令（例如 pytest）来运行单元测试，验证重构后的代码是否符合质量标准。
    """
    try:
        encoding_format = "gbk" if os.name == "nt" else "utf-8"
        result = subprocess.run(
            test_command,
            shell=True,
            capture_output=True,
            text=True,
            encoding=encoding_format,
            errors="replace",
            timeout=20,
        )
        output = (result.stdout or "") + "\n" + (result.stderr or "")
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
