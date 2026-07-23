from pathlib import Path

import code_indexer
from graph_indexer import parse_code_to_graph


def _write_source(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_symbol_index_preserves_same_named_methods(tmp_path: Path):
    source_root = tmp_path / "CodeSmells"
    _write_source(
        source_root / "alpha.py",
        "class Alpha:\n    def run(self):\n        return 1\n",
    )
    _write_source(
        source_root / "beta.py",
        "class Beta:\n    def run(self):\n        return 2\n",
    )

    assert code_indexer.index_directory(source_root) == 4
    result = code_indexer.get_symbol_definition_content("run")

    assert "Alpha.run" in result
    assert "Beta.run" in result
    assert result.count("限定名称:") == 2


def test_incremental_file_refresh_replaces_stale_source(tmp_path: Path):
    source_root = tmp_path / "CodeSmells"
    source_file = source_root / "service.py"
    _write_source(source_file, "def calculate():\n    return 'old'\n")
    code_indexer.index_directory(source_root)

    _write_source(source_file, "def calculate():\n    return 'new'\n")
    assert code_indexer.index_file(source_file, source_root) == 1

    result = code_indexer.get_symbol_definition_content("calculate")
    assert "return 'new'" in result
    assert "return 'old'" not in result


def test_graph_uses_stable_ids_and_resolves_same_class_calls(tmp_path: Path):
    source_root = tmp_path / "CodeSmells"
    _write_source(
        source_root / "workers.py",
        "\n".join(
            [
                "class Alpha:",
                "    def run(self):",
                "        return 1",
                "    def execute(self):",
                "        return self.run()",
                "",
                "class Beta:",
                "    def run(self):",
                "        return 2",
            ]
        ),
    )

    symbols, calls = parse_code_to_graph(source_root)
    ids = {symbol["qualname"]: symbol["id"] for symbol in symbols}

    assert ids["Alpha.run"] != ids["Beta.run"]
    assert (ids["Alpha.execute"], ids["Alpha.run"]) in calls
    assert (ids["Alpha.execute"], ids["Beta.run"]) not in calls


def test_class_symbol_does_not_duplicate_method_calls(tmp_path: Path):
    source_root = tmp_path / "CodeSmells"
    _write_source(
        source_root / "service.py",
        "\n".join(
            [
                "def helper():",
                "    return 1",
                "",
                "class Service:",
                "    def execute(self):",
                "        return helper()",
            ]
        ),
    )

    symbols, calls = parse_code_to_graph(source_root)
    ids = {symbol["qualname"]: symbol["id"] for symbol in symbols}

    assert (ids["Service.execute"], ids["helper"]) in calls
    assert (ids["Service"], ids["helper"]) not in calls
