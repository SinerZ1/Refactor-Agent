import ast
import os
from pathlib import Path
from typing import Any, TypedDict

from dotenv import load_dotenv
from neo4j import GraphDatabase

from code_indexer import (
    display_file_path,
    resolve_source_directory,
    symbol_identity,
)

load_dotenv()

NEO4J_PROJECT_ID = os.getenv("NEO4J_PROJECT_ID", "refactor-agent")

_neo4j_health = {
    "status": "degraded",
    "backend": "ast",
    "reason": "not_configured",
}


class GraphSymbol(TypedDict):
    id: str
    name: str
    qualname: str
    type: str
    file_path: str
    code: str
    node_ref: ast.AST


class TopologyData(TypedDict):
    nodes: list[dict[str, Any]]
    links: list[dict[str, str]]
    fallback: bool


def get_neo4j_driver():
    neo4j_uri = os.getenv("NEO4J_URI", "bolt://localhost:7687").strip()
    neo4j_user = os.getenv("NEO4J_USER", "").strip()
    neo4j_password = os.getenv("NEO4J_PASSWORD", "")
    if not neo4j_user or not neo4j_password:
        _neo4j_health.update(
            status="degraded", backend="ast", reason="credentials_not_configured"
        )
        print("[Neo4j] 未配置用户名或密码，使用内存 AST 调用图。")
        return None
    driver = None
    try:
        driver = GraphDatabase.driver(
            neo4j_uri,
            auth=(neo4j_user, neo4j_password),
        )
        driver.verify_connectivity()
        _neo4j_health.update(status="ok", backend="neo4j", reason="available")
        return driver
    except Exception as exc:
        _neo4j_health.update(
            status="degraded",
            backend="ast",
            reason=f"neo4j_unavailable:{exc.__class__.__name__}",
        )
        # 驱动异常可能复述认证 URI；日志只保留异常类型，避免口令被拼入输出。
        print(f"[Neo4j] 连接失败 ({exc.__class__.__name__})，使用内存 AST 调用图。")
        if driver is not None:
            driver.close()
        return None


def get_neo4j_health() -> dict[str, str]:
    return dict(_neo4j_health)


class _GraphSymbolVisitor(ast.NodeVisitor):
    def __init__(
        self, file_path: Path, source_root: Path, source_lines: list[str]
    ) -> None:
        self.file_path = file_path
        self.source_root = source_root
        self.source_lines = source_lines
        self.scope: list[str] = []
        self.symbols: list[GraphSymbol] = []

    def _visit_symbol(
        self, node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
    ) -> None:
        qualname = ".".join([*self.scope, node.name])
        end_line = node.end_lineno or len(self.source_lines)
        self.symbols.append(
            {
                "id": symbol_identity(self.file_path, self.source_root, qualname),
                "name": node.name,
                "qualname": qualname,
                "type": "Class" if isinstance(node, ast.ClassDef) else "Function",
                "file_path": display_file_path(self.file_path, self.source_root),
                "code": "\n".join(self.source_lines[node.lineno - 1 : end_line]),
                "node_ref": node,
            }
        )
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._visit_symbol(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_symbol(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_symbol(node)


class _DirectCallCollector(ast.NodeVisitor):
    """只收集当前符号直接拥有的调用，跳过嵌套类和函数以免重复归因。"""

    def __init__(self, root: ast.AST) -> None:
        self.root = root
        self.calls: list[ast.Call] = []

    def visit_Call(self, node: ast.Call) -> None:
        self.calls.append(node)
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        if node is self.root:
            for statement in node.body:
                if not isinstance(
                    statement, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
                ):
                    self.visit(statement)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        if node is self.root:
            for statement in node.body:
                self.visit(statement)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        if node is self.root:
            for statement in node.body:
                self.visit(statement)


def _resolve_callee(
    caller: GraphSymbol,
    call: ast.Call,
    symbols_by_name: dict[str, list[GraphSymbol]],
) -> str | None:
    if isinstance(call.func, ast.Name):
        called_name = call.func.id
    elif isinstance(call.func, ast.Attribute):
        called_name = call.func.attr
    else:
        return None

    candidates = symbols_by_name.get(called_name, [])
    if not candidates:
        return None

    caller_parent = caller["qualname"].rsplit(".", 1)[0]
    same_scope = [
        candidate
        for candidate in candidates
        if candidate["file_path"] == caller["file_path"]
        and candidate["qualname"].rsplit(".", 1)[0] == caller_parent
    ]
    if len(same_scope) == 1:
        return same_scope[0]["id"]

    same_file_module = [
        candidate
        for candidate in candidates
        if candidate["file_path"] == caller["file_path"]
        and "." not in candidate["qualname"]
    ]
    if len(same_file_module) == 1:
        return same_file_module[0]["id"]
    if len(candidates) == 1:
        return candidates[0]["id"]
    return None


def parse_code_to_graph(
    directory_path: str | Path = "CodeSmells",
) -> tuple[list[GraphSymbol], list[tuple[str, str]]]:
    """解析符号与直接调用关系，所有节点和边均使用稳定 symbol_id。"""

    source_root = resolve_source_directory(directory_path)
    if not source_root.is_dir():
        return [], []

    symbols: list[GraphSymbol] = []
    for file_path in source_root.rglob("*.py"):
        try:
            source_code = file_path.read_text(encoding="utf-8")
            visitor = _GraphSymbolVisitor(
                file_path, source_root, source_code.splitlines()
            )
            visitor.visit(ast.parse(source_code))
            symbols.extend(visitor.symbols)
        except (OSError, SyntaxError, UnicodeError) as exc:
            print(f"[Graph Indexer] 解析文件 {file_path} 异常: {exc}")

    symbols_by_name: dict[str, list[GraphSymbol]] = {}
    for symbol in symbols:
        symbols_by_name.setdefault(symbol["name"], []).append(symbol)

    calls: set[tuple[str, str]] = set()
    for symbol in symbols:
        collector = _DirectCallCollector(symbol["node_ref"])
        collector.visit(symbol["node_ref"])
        for call in collector.calls:
            callee_id = _resolve_callee(symbol, call, symbols_by_name)
            if callee_id and callee_id != symbol["id"]:
                calls.add((symbol["id"], callee_id))
    return symbols, sorted(calls)


def _replace_project_graph(tx, symbols: list[dict[str, Any]], calls: list[dict]):
    """在单事务内替换本项目子图，失败时保留旧快照。"""

    tx.run(
        "MATCH (n:Symbol {project_id: $project_id}) DETACH DELETE n",
        project_id=NEO4J_PROJECT_ID,
    )
    tx.run(
        """
        UNWIND $symbols AS symbol
        MERGE (s:Symbol {project_id: $project_id, id: symbol.id})
        SET s.name = symbol.name,
            s.qualname = symbol.qualname,
            s.type = symbol.type,
            s.file_path = symbol.file_path,
            s.code = symbol.code
        """,
        project_id=NEO4J_PROJECT_ID,
        symbols=symbols,
    )
    tx.run(
        """
        UNWIND $calls AS call
        MATCH (a:Symbol {project_id: $project_id, id: call.source})
        MATCH (b:Symbol {project_id: $project_id, id: call.target})
        MERGE (a)-[:CALLS]->(b)
        """,
        project_id=NEO4J_PROJECT_ID,
        calls=calls,
    )


def index_to_neo4j(directory_path: str | Path = "CodeSmells") -> bool:
    driver = get_neo4j_driver()
    if not driver:
        print("[Neo4j] 跳过图索引写入，因为驱动无法加载。")
        return False

    symbols, calls = parse_code_to_graph(directory_path)
    serializable_symbols = [
        {key: value for key, value in symbol.items() if key != "node_ref"}
        for symbol in symbols
    ]
    serializable_calls = [
        {"source": source, "target": target} for source, target in calls
    ]
    try:
        with driver.session() as session:
            session.execute_write(
                _replace_project_graph, serializable_symbols, serializable_calls
            )
        print(f"[Neo4j] 成功写入 {len(symbols)} 个符号，{len(calls)} 条调用关系。")
        return True
    except Exception as exc:
        _neo4j_health.update(
            status="degraded",
            backend="ast",
            reason=f"neo4j_write_failed:{exc.__class__.__name__}",
        )
        print(f"[Neo4j] 写入图谱数据失败 ({exc.__class__.__name__})，保留 AST 降级。")
        return False
    finally:
        driver.close()


def _fallback_topology() -> TopologyData:
    symbols, calls = parse_code_to_graph()
    return {
        "nodes": [
            {
                key: symbol[key]
                for key in ("id", "name", "qualname", "type", "file_path")
            }
            for symbol in symbols
        ],
        "links": [{"source": source, "target": target} for source, target in calls],
        "fallback": True,
    }


def get_topology_data() -> TopologyData:
    driver = get_neo4j_driver()
    if not driver:
        return _fallback_topology()

    nodes: list[dict[str, Any]] = []
    links: list[dict[str, str]] = []
    try:
        with driver.session() as session:
            result = session.run(
                "MATCH (n:Symbol {project_id: $project_id}) RETURN n",
                project_id=NEO4J_PROJECT_ID,
            )
            for record in result:
                node = record["n"]
                nodes.append(
                    {
                        "id": node.get("id"),
                        "name": node.get("name"),
                        "qualname": node.get("qualname"),
                        "type": node.get("type"),
                        "file_path": node.get("file_path"),
                        "code": node.get("code"),
                    }
                )

            result = session.run(
                """
                MATCH (a:Symbol {project_id: $project_id})-[:CALLS]->
                      (b:Symbol {project_id: $project_id})
                RETURN a.id AS source, b.id AS target
                """,
                project_id=NEO4J_PROJECT_ID,
            )
            links.extend(
                {"source": record["source"], "target": record["target"]}
                for record in result
            )
    except Exception as exc:
        _neo4j_health.update(
            status="degraded",
            backend="ast",
            reason=f"neo4j_query_failed:{exc.__class__.__name__}",
        )
        print(f"[Neo4j] 获取拓扑图谱失败 ({exc.__class__.__name__})，返回 AST 图。")
        return _fallback_topology()
    finally:
        driver.close()
    return {"nodes": nodes, "links": links, "fallback": False}


if __name__ == "__main__":
    index_to_neo4j()
