import ast
import os

# 全局内存符号索引：用于存储项目里类与函数的定义
SYMBOL_INDEX = {}


def index_directory(directory_path: str = "CodeSmells"):
    """
    利用 Python ast (抽象语法树) 静态解析目录下的所有 python 文件，提取类和函数定义
    """
    global SYMBOL_INDEX
    SYMBOL_INDEX.clear()

    # 动态适应工作目录：如果是从 backend 目录下运行，符号目录应该在 ../CodeSmells
    if not os.path.exists(directory_path):
        if os.path.exists("../CodeSmells"):
            directory_path = "../CodeSmells"
        elif os.path.exists("backend/CodeSmells"):
            directory_path = "backend/CodeSmells"
        else:
            return

    if False:  # 跳过原有的检测逻辑
        # 兼容单独在 backend 目录下运行的情况
        if directory_path.startswith("backend/") and os.path.exists(
            directory_path.replace("backend/", "")
        ):
            directory_path = directory_path.replace("backend/", "")
        else:
            return

    for root, _, files in os.walk(directory_path):
        for file in files:
            if file.endswith(".py"):
                file_path = os.path.join(root, file)
                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        source_code = f.read()

                    # 利用 AST 将代码解析为语法树
                    tree = ast.parse(source_code)
                    lines = source_code.splitlines()

                    # 遍历语法树中所有节点
                    for node in ast.walk(tree):
                        # 仅关注 Class (类定义) 与 Function (函数定义)
                        if isinstance(node, (ast.ClassDef, ast.FunctionDef)):
                            start_line = node.lineno
                            # getattr 兼容旧版本 python 的 end_lineno 属性
                            end_line = getattr(node, "end_lineno", len(lines))

                            symbol_name = node.name
                            symbol_code = "\n".join(lines[start_line - 1 : end_line])

                            # 建立符号关联索引
                            SYMBOL_INDEX[symbol_name] = {
                                "file_path": file_path,
                                "name": symbol_name,
                                "type": (
                                    "Class"
                                    if isinstance(node, ast.ClassDef)
                                    else "Function"
                                ),
                                "code": symbol_code,
                            }
                except Exception as e:
                    print(f"[Indexer] 静态解析文件 {file_path} 异常: {e}")


def get_symbol_definition_content(symbol_name: str) -> str:
    """
    根据符号名(例如类名、函数名)直接精准定位、返回源码。
    """
    global SYMBOL_INDEX

    # 动态扫描两个默认路径 (兼容根目录启动和 backend 目录下启动)
    if not SYMBOL_INDEX:
        if os.path.exists("CodeSmells"):
            index_directory("CodeSmells")
        elif os.path.exists("../CodeSmells"):
            index_directory("../CodeSmells")
        elif os.path.exists("backend/CodeSmells"):
            index_directory("backend/CodeSmells")

    symbol = SYMBOL_INDEX.get(symbol_name)
    if symbol:
        return (
            f"=== [AST RAG RETAINED] 检索到 `{symbol_name}` 的定义与源码如下 ===\n"
            f"文件路径: {symbol['file_path']}\n"
            f"类型: {symbol['type']}\n"
            f"--------------------------------------------------\n"
            f"{symbol['code']}\n"
            f"=================================================="
        )
    return f"未能在本地项目的 AST 索引中检索到符号 `{symbol_name}` 的声明。"
