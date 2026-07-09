import os
import ast
from neo4j import GraphDatabase
from dotenv import load_dotenv

load_dotenv()

# 从环境变量加载 Neo4j 配置
NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "password")

def get_neo4j_driver():
    try:
        driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
        # 测试连接
        driver.verify_connectivity()
        return driver
    except Exception as e:
        print(f"[Neo4j] 连接失败: {e}. 请确保 Neo4j 正在运行并且配置正确。")
        return None

def parse_code_to_graph(directory_path: str = "CodeSmells"):
    """
    解析指定目录下的 Python 代码，提取符号（类、函数）定义以及它们内部的调用关系。
    """
    # 动态适应工作目录
    if not os.path.exists(directory_path):
        if os.path.exists("../CodeSmells"):
            directory_path = "../CodeSmells"
        elif os.path.exists("backend/CodeSmells"):
            directory_path = "backend/CodeSmells"
        else:
            return [], []

    symbols = []
    calls = []

    # 存储符号的字典，用于快速查找 caller 关系
    symbol_dict = {}

    # 1. 扫描所有符号
    for root, _, files in os.walk(directory_path):
        for file in files:
            if file.endswith(".py"):
                file_path = os.path.join(root, file)
                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        source_code = f.read()

                    tree = ast.parse(source_code)
                    lines = source_code.splitlines()

                    for node in ast.walk(tree):
                        if isinstance(node, (ast.ClassDef, ast.FunctionDef)):
                            start_line = node.lineno
                            end_line = getattr(node, "end_lineno", len(lines))
                            symbol_name = node.name
                            symbol_code = "\n".join(lines[start_line - 1 : end_line])
                            stype = "Class" if isinstance(node, ast.ClassDef) else "Function"
                            
                            # 收集符号
                            symbol_info = {
                                "name": symbol_name,
                                "type": stype,
                                "file_path": file_path.replace("\\", "/"),
                                "code": symbol_code,
                                "node_ref": node # 保存 ast 节点以在下一步寻找调用
                            }
                            symbols.append(symbol_info)
                            symbol_dict[symbol_name] = symbol_info
                except Exception as e:
                    print(f"[Graph Indexer] 解析文件 {file_path} 异常: {e}")

    # 2. 扫描符号内部的调用关系
    for sym in symbols:
        node = sym["node_ref"]
        # 遍历当前符号节点内部的所有子节点
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                # 如果是直接调用函数名，如 foo()
                if isinstance(child.func, ast.Name):
                    called_name = child.func.id
                    if called_name in symbol_dict and called_name != sym["name"]:
                        calls.append((sym["name"], called_name))
                # 如果是通过对象调用，如 self.foo() 或 service.foo()
                elif isinstance(child.func, ast.Attribute):
                    called_name = child.func.attr
                    if called_name in symbol_dict and called_name != sym["name"]:
                        calls.append((sym["name"], called_name))

    # 去重
    calls = list(set(calls))
    return symbols, calls

def index_to_neo4j(directory_path: str = "CodeSmells"):
    """
    静态解析代码，然后将类、函数和调用关系存入 Neo4j
    """
    driver = get_neo4j_driver()
    if not driver:
        print("[Neo4j] 跳过图索引写入，因为驱动无法加载。")
        return False

    symbols, calls = parse_code_to_graph(directory_path)
    
    try:
        with driver.session() as session:
            # 1. 清空旧数据（仅清理代码相关的图）
            session.run("MATCH (n:Symbol) DETACH DELETE n")
            
            # 2. 写入节点
            for sym in symbols:
                session.run(
                    """
                    MERGE (s:Symbol {name: $name})
                    SET s.type = $type,
                        s.file_path = $file_path,
                        s.code = $code
                    """,
                    name=sym["name"],
                    type=sym["type"],
                    file_path=sym["file_path"],
                    code=sym["code"]
                )
            
            # 3. 写入调用边
            for caller, callee in calls:
                session.run(
                    """
                    MATCH (a:Symbol {name: $caller})
                    MATCH (b:Symbol {name: $callee})
                    MERGE (a)-[:CALLS]->(b)
                    """,
                    caller=caller,
                    callee=callee
                )
        print(f"[Neo4j] 成功写入 {len(symbols)} 个符号，{len(calls)} 条调用关系。")
        return True
    except Exception as e:
        print(f"[Neo4j] 写入图谱数据失败: {e}")
        return False
    finally:
        driver.close()

def get_topology_data():
    """
    提供给前端的可视化接口：查询 Neo4j 拓扑结构，返回节点和边。
    """
    driver = get_neo4j_driver()
    if not driver:
        # 降级返回空，或者基于 AST 静态结果构建内存图
        symbols, calls = parse_code_to_graph()
        nodes = [{"name": s["name"], "type": s["type"], "file_path": s["file_path"]} for s in symbols]
        links = [{"source": c[0], "target": c[1]} for c in calls]
        return {"nodes": nodes, "links": links, "fallback": True}

    nodes = []
    links = []
    try:
        with driver.session() as session:
            result = session.run("MATCH (n:Symbol) RETURN n")
            for record in result:
                node = record["n"]
                nodes.append({
                    "name": node.get("name"),
                    "type": node.get("type"),
                    "file_path": node.get("file_path"),
                    "code": node.get("code")
                })
            
            result = session.run("MATCH (a:Symbol)-[r:CALLS]->(b:Symbol) RETURN a.name AS source, b.name AS target")
            for record in result:
                links.append({
                    "source": record["source"],
                    "target": record["target"]
                })
    except Exception as e:
        print(f"[Neo4j] 获取拓扑图谱失败: {e}")
        # 异常情况下也采用 AST 降级
        symbols, calls = parse_code_to_graph()
        nodes = [{"name": s["name"], "type": s["type"], "file_path": s["file_path"]} for s in symbols]
        links = [{"source": c[0], "target": c[1]} for c in calls]
        return {"nodes": nodes, "links": links, "fallback": True}
    finally:
        driver.close()
        
    return {"nodes": nodes, "links": links, "fallback": False}

if __name__ == "__main__":
    index_to_neo4j()
