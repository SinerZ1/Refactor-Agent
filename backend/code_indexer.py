import ast
from pathlib import Path
from threading import RLock
from typing import TypedDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SOURCE_ROOT = PROJECT_ROOT / "CodeSmells"


class SymbolRecord(TypedDict):
    id: str
    name: str
    qualname: str
    type: str
    file_path: str
    code: str


# 索引使用稳定 symbol_id 作为主键，避免不同类或文件中的同名方法互相覆盖。
SYMBOL_INDEX: dict[str, SymbolRecord] = {}
_INDEX_LOCK = RLock()


def resolve_source_directory(directory_path: str | Path = "CodeSmells") -> Path:
    """把调用方给出的源码目录归一化为稳定绝对路径。"""

    candidate = Path(directory_path)
    if not candidate.is_absolute():
        project_candidate = PROJECT_ROOT / candidate
        backend_candidate = Path(__file__).resolve().parent / candidate
        candidate = (
            project_candidate
            if project_candidate.exists()
            else backend_candidate if backend_candidate.exists() else project_candidate
        )
    return candidate.resolve()


def display_file_path(file_path: Path, source_root: Path) -> str:
    """优先返回项目相对路径，临时测试目录则相对其源码根目录展示。"""

    resolved_file = file_path.resolve()
    try:
        return resolved_file.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return (
            Path(source_root.name) / resolved_file.relative_to(source_root)
        ).as_posix()


def symbol_identity(file_path: Path, source_root: Path, qualname: str) -> str:
    return f"{display_file_path(file_path, source_root)}:{qualname}"


class _SymbolVisitor(ast.NodeVisitor):
    """按词法作用域生成 Python qualified name，而不是依赖易冲突的裸名称。"""

    def __init__(
        self, file_path: Path, source_root: Path, source_lines: list[str]
    ) -> None:
        self.file_path = file_path
        self.source_root = source_root
        self.source_lines = source_lines
        self.scope: list[str] = []
        self.symbols: list[SymbolRecord] = []

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


def scan_python_file(file_path: Path, source_root: Path) -> list[SymbolRecord]:
    source_code = file_path.read_text(encoding="utf-8")
    tree = ast.parse(source_code)
    visitor = _SymbolVisitor(file_path, source_root, source_code.splitlines())
    visitor.visit(tree)
    return visitor.symbols


def index_directory(directory_path: str | Path = "CodeSmells") -> int:
    """重建目录符号索引，并原子替换内存快照。"""

    source_root = resolve_source_directory(directory_path)
    if not source_root.is_dir():
        return 0

    next_index: dict[str, SymbolRecord] = {}
    for file_path in source_root.rglob("*.py"):
        try:
            for symbol in scan_python_file(file_path, source_root):
                next_index[symbol["id"]] = symbol
        except (OSError, SyntaxError, UnicodeError) as exc:
            print(f"[Indexer] 静态解析文件 {file_path} 异常: {exc}")

    with _INDEX_LOCK:
        SYMBOL_INDEX.clear()
        SYMBOL_INDEX.update(next_index)
    return len(next_index)


def index_file(file_path: str | Path, directory_path: str | Path = "CodeSmells") -> int:
    """增量替换单文件符号，供 HITL 批准写入后立即刷新 RAG。"""

    source_root = resolve_source_directory(directory_path)
    resolved_file = Path(file_path).resolve()
    display_path = display_file_path(resolved_file, source_root)
    symbols = scan_python_file(resolved_file, source_root)

    with _INDEX_LOCK:
        stale_ids = [
            symbol_id
            for symbol_id, symbol in SYMBOL_INDEX.items()
            if symbol["file_path"] == display_path
        ]
        for symbol_id in stale_ids:
            SYMBOL_INDEX.pop(symbol_id, None)
        for symbol in symbols:
            SYMBOL_INDEX[symbol["id"]] = symbol
    return len(symbols)


def get_symbol_definition_content(symbol_name: str) -> str:
    """按 ID、qualified name 或裸名称检索；同名结果全部返回而不静默覆盖。"""

    if not SYMBOL_INDEX:
        index_directory()

    query = symbol_name.strip()
    with _INDEX_LOCK:
        matches = [
            symbol
            for symbol in SYMBOL_INDEX.values()
            if query in {symbol["id"], symbol["qualname"], symbol["name"]}
        ]

    if not matches:
        return f"未能在本地项目的 AST 索引中检索到符号 `{query}` 的声明。"

    sections = []
    for symbol in sorted(matches, key=lambda item: item["id"]):
        sections.append(
            "\n".join(
                [
                    f"=== [AST RAG RETAINED] `{symbol['id']}` ===",
                    f"文件路径: {symbol['file_path']}",
                    f"限定名称: {symbol['qualname']}",
                    f"类型: {symbol['type']}",
                    "--------------------------------------------------",
                    symbol["code"],
                    "==================================================",
                ]
            )
        )
    return "\n\n".join(sections)
