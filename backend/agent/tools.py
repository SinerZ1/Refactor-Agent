import os
import subprocess
from langchain_core.tools import tool

# ============================================================
# 教学说明: 智能体工具库 (Agent Tooling)
# ------------------------------------------------------------
# 这里的工具在底层相当于 Hello-Agents 课程里讲过的 Tool 定义。
# 通过 @tool 装饰器，LangChain 能够自动根据函数签名和 Docstring 
# 提取出 JSON Schema，并在 LLM 调用时序列化并传输，实现自动函数调用。
# ============================================================

@tool
def read_code_file(file_path: str) -> str:
    """
    读取指定路径下的本地代码文件内容。当需要查看某个具体文件的代码时使用。
    """
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        return f"读取文件失败: {str(e)}"

@tool
def write_code_file(file_path: str, content: str) -> str:
    """
    将重构后的完整代码写入到指定的本地文件路径中。当重构完成并且需要保存修改时使用。
    """
    try:
        dir_name = os.path.dirname(os.path.abspath(file_path))
        if dir_name:
            os.makedirs(dir_name, exist_ok=True)
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(content)
        return f"成功将重构代码写入到文件: {file_path}"
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
            timeout=20
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
            result_str += f"- [{node['type']}] {node['name']} (定义于 {node['file_path']})\n"
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
