import ast
from collections.abc import Mapping
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


class SymbolIndexSnapshot(TypedDict):
    """一次目录解析的不可变语义快照，不写入进程级全局状态。"""

    symbols: dict[str, SymbolRecord]
    parse_errors: list[str]


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
    """隐藏物理工作区位置，只暴露稳定的源码根相对路径。"""

    resolved_file = file_path.resolve()
    return (Path(source_root.name) / resolved_file.relative_to(source_root)).as_posix()


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


def build_symbol_index(
    directory_path: str | Path = "CodeSmells",
) -> SymbolIndexSnapshot:
    """按需解析一个源码根，返回调用方私有索引而不触碰全局状态。"""

    source_root = resolve_source_directory(directory_path)
    if not source_root.is_dir():
        return {"symbols": {}, "parse_errors": ["源码目录不存在"]}

    next_index: dict[str, SymbolRecord] = {}
    parse_errors: list[str] = []
    for file_path in sorted(source_root.rglob("*.py")):
        try:
            for symbol in scan_python_file(file_path, source_root):
                next_index[symbol["id"]] = symbol
        except (OSError, SyntaxError, UnicodeError) as exc:
            display_path = display_file_path(file_path, source_root)
            parse_errors.append(f"{display_path}:{exc.__class__.__name__}")
    return {"symbols": next_index, "parse_errors": parse_errors}


def index_directory(directory_path: str | Path = "CodeSmells") -> int:
    """重建原始源码全局索引，并原子替换内存快照。"""

    snapshot = build_symbol_index(directory_path)
    for parse_error in snapshot["parse_errors"]:
        print(f"[Indexer] 静态解析异常: {parse_error}")

    with _INDEX_LOCK:
        SYMBOL_INDEX.clear()
        SYMBOL_INDEX.update(snapshot["symbols"])
    return len(snapshot["symbols"])


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


def render_symbol_definition_content(
    symbol_name: str,
    symbols: Mapping[str, SymbolRecord],
    *,
    parse_errors: list[str] | None = None,
) -> str:
    """从显式索引快照检索，便于 run 级依赖注入而非切换模块全局。"""

    query = symbol_name.strip()
    matches = [
        symbol
        for symbol in symbols.values()
        if query in {symbol["id"], symbol["qualname"], symbol["name"]}
    ]

    if not matches:
        result = f"未能在指定 AST 索引中检索到符号 `{query}` 的声明。"
        if parse_errors:
            result += "\n[AST_PARSE_WARNINGS] " + "；".join(parse_errors[:10])
        return result

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
    if parse_errors:
        sections.append("[AST_PARSE_WARNINGS] " + "；".join(parse_errors[:10]))
    return "\n\n".join(sections)


def get_symbol_definition_content(symbol_name: str) -> str:
    """查询用户原始源码索引；该全局快照永不指向临时 run 工作区。"""

    if not SYMBOL_INDEX:
        index_directory()
    with _INDEX_LOCK:
        snapshot = dict(SYMBOL_INDEX)
    return render_symbol_definition_content(symbol_name, snapshot)


def get_workspace_symbol_definition_content(
    symbol_name: str,
    source_root: str | Path,
    *,
    allowed_paths: set[str] | None = None,
) -> tuple[str, SymbolIndexSnapshot]:
    """查询时解析当前 working tree，天然反映写入、删除、重命名与语法错误。"""

    snapshot = build_symbol_index(source_root)
    symbols: Mapping[str, SymbolRecord] = snapshot["symbols"]
    if allowed_paths is not None:
        allowed_identities = {path.casefold() for path in allowed_paths}
        symbols = {
            symbol_id: symbol
            for symbol_id, symbol in symbols.items()
            if symbol["file_path"].casefold() in allowed_identities
        }
    return (
        render_symbol_definition_content(
            symbol_name,
            symbols,
            parse_errors=snapshot["parse_errors"],
        ),
        snapshot,
    )
