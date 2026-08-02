import atexit
import difflib
import hashlib
import json
import os
import re
import shutil
import tempfile
import uuid
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from typing import Any, Literal, TypedDict

from langgraph.errors import GraphInterrupt
from langgraph.types import interrupt

from .path_policy import PathPolicyError, canonical_refactor_path, is_protected_path
from .state import State

PROJECT_ROOT = Path(__file__).resolve().parents[2]
REFACTOR_ROOT = (PROJECT_ROOT / "CodeSmells").resolve()
WORKSPACE_ROOT = (PROJECT_ROOT / ".refactor-workspaces").resolve()
MAX_AGGREGATE_DIFF_CHARS = 500_000
_WORKSPACE_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
SNAPSHOT_VERSION: Literal[1] = 1
SNAPSHOT_IGNORED_DIRECTORIES = frozenset({"__pycache__", ".pytest_cache"})
SNAPSHOT_IGNORED_FILE_NAMES = frozenset({".coverage"})


class WorkspaceFileSnapshot(TypedDict):
    """源码快照中的单个确定性文件事实。"""

    path: str
    file_type: Literal["regular"]
    size_bytes: int
    sha256: str


class WorkspaceSnapshot(TypedDict):
    """完整 managed source tree 的内容寻址清单。"""

    version: Literal[1]
    files: list[WorkspaceFileSnapshot]
    digest: str


class WorkspaceError(RuntimeError):
    """隔离工作区生命周期中的确定性失败。"""


class BaselineConflictError(WorkspaceError):
    """真实文件已偏离 run 启动时的基线，禁止静默覆盖。"""


class AtomicApplyError(WorkspaceError):
    """原子应用失败，并携带补偿事务是否完整完成。"""

    def __init__(self, message: str, *, rolled_back: bool):
        super().__init__(message)
        self.rolled_back = rolled_back


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _safe_workspace_dir(workspace_id: str) -> Path:
    """把清理与访问约束为工作区根目录的直接子目录。

    删除临时目录是高风险能力，因此不能只依赖 ``is_relative_to``。同时校验随机 ID
    格式、解析后的父目录和目录名，等价于为资源回收建立 capability boundary，
    避免损坏状态或路径穿越把 ``shutil.rmtree`` 扩大到仓库其他位置。
    """

    if not _WORKSPACE_ID_PATTERN.fullmatch(workspace_id):
        raise WorkspaceError("工作区 ID 非法")
    root = WORKSPACE_ROOT.resolve(strict=False)
    project_root = PROJECT_ROOT.resolve(strict=False)
    if not root.is_relative_to(project_root) or root.name != ".refactor-workspaces":
        raise WorkspaceError("工作区根目录越出项目范围")
    candidate = (root / workspace_id).resolve(strict=False)
    if candidate.parent != root or candidate.name != workspace_id:
        raise WorkspaceError("工作区路径越出指定运行目录")
    return candidate


def _snapshot_root(workspace_id: str, snapshot: str) -> Path:
    if snapshot not in {"baseline", "working"}:
        raise WorkspaceError("工作区快照类型非法")
    workspace_dir = _safe_workspace_dir(workspace_id)
    candidate = (workspace_dir / snapshot).resolve(strict=False)
    if candidate.parent != workspace_dir:
        raise WorkspaceError("工作区快照路径越界")
    return candidate


def _canonical_relative_path(file_path: str) -> Path:
    try:
        normalized = canonical_refactor_path(
            file_path,
            project_root=PROJECT_ROOT,
            refactor_root=REFACTOR_ROOT,
        )
    except PathPolicyError as exc:
        raise WorkspaceError(str(exc)) from exc
    return Path(normalized)


def _assert_no_protected_changes(
    baseline_files: dict[str, Path],
    final_files: dict[str, Path],
) -> None:
    """在候选集合边界再次检查可信测试，覆盖删除、创建、修改与重命名。"""

    for relative in sorted(set(baseline_files) | set(final_files)):
        before = baseline_files.get(relative)
        after = final_files.get(relative)
        before_bytes = before.read_bytes() if before is not None else None
        after_bytes = after.read_bytes() if after is not None else None
        if before_bytes == after_bytes:
            continue
        try:
            protected = is_protected_path(relative, project_root=PROJECT_ROOT)
        except PathPolicyError as exc:
            raise WorkspaceError(str(exc)) from exc
        if protected:
            raise WorkspaceError(f"候选变更触及可信行为契约测试 `{relative}`，拒绝继续")


def resolve_workspace_path(workspace_id: str, file_path: str) -> Path:
    relative_path = _canonical_relative_path(file_path)
    working_root = _snapshot_root(workspace_id, "working")
    candidate = (working_root / relative_path).resolve(strict=False)
    allowed_root = (working_root / "CodeSmells").resolve(strict=False)
    if not candidate.is_relative_to(allowed_root):
        raise WorkspaceError("文件路径超出当前 run 的隔离工作区")
    return candidate


def _iter_snapshot_files(snapshot_root: Path) -> dict[str, Path]:
    code_root = snapshot_root / "CodeSmells"
    if not code_root.is_dir():
        raise WorkspaceError("隔离工作区缺少 CodeSmells 快照")
    files: dict[str, Path] = {}
    for path in code_root.rglob("*"):
        relative_parts = path.relative_to(code_root).parts
        if any(part in SNAPSHOT_IGNORED_DIRECTORIES for part in relative_parts):
            continue
        if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
            raise WorkspaceError("隔离工作区包含符号链接或目录联接，拒绝生成快照")
        if path.is_file():
            if (
                path.suffix.casefold() == ".pyc"
                or path.name in SNAPSHOT_IGNORED_FILE_NAMES
                or path.name.startswith(".coverage.")
            ):
                continue
            relative = path.relative_to(snapshot_root).as_posix()
            files[relative] = path
    return files


def _normalize_snapshot_path(relative_path: str) -> str:
    """统一 Windows/POSIX 表达，并拒绝快照清单中的路径歧义。"""

    normalized = PurePosixPath(relative_path.replace("\\", "/"))
    if normalized.is_absolute() or ".." in normalized.parts:
        raise WorkspaceError("工作区快照包含非法相对路径")
    canonical = normalized.as_posix()
    if not canonical.startswith("CodeSmells/"):
        raise WorkspaceError("工作区快照包含非 CodeSmells 文件")
    return canonical


def build_snapshot_manifest(
    entries: Iterable[tuple[str, bytes]],
) -> WorkspaceSnapshot:
    """从完整文件序列构造顺序无关、时间无关的内容寻址清单。

    这对应可复现构建中的 Merkle-root 思路：操作日志可以被截断用于 UI，但测试证据
    绑定的是所有受管理文件的规范路径、类型、大小与内容哈希。摘要不包含 mtime、枚举
    顺序或平台分隔符，因此同一候选树在 Windows 与 POSIX 上具有相同身份。
    """

    files_by_identity: dict[str, WorkspaceFileSnapshot] = {}
    for relative_path, content in entries:
        canonical = _normalize_snapshot_path(relative_path)
        identity = canonical.casefold()
        if identity in files_by_identity:
            raise WorkspaceError(f"工作区快照包含大小写冲突路径: {canonical}")
        files_by_identity[identity] = {
            "path": canonical,
            "file_type": "regular",
            "size_bytes": len(content),
            "sha256": _sha256_bytes(content),
        }
    files = sorted(
        files_by_identity.values(),
        key=lambda item: (item["path"].casefold(), item["path"]),
    )
    payload = {"version": SNAPSHOT_VERSION, "files": files}
    canonical_json = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return {
        "version": SNAPSHOT_VERSION,
        "files": files,
        "digest": _sha256_bytes(canonical_json.encode("utf-8")),
    }


def build_workspace_snapshot(
    workspace_id: str,
    snapshot: Literal["baseline", "working"] = "working",
) -> WorkspaceSnapshot:
    snapshot_root = _snapshot_root(workspace_id, snapshot)
    files = _iter_snapshot_files(snapshot_root)
    return build_snapshot_manifest(
        (relative, path.read_bytes()) for relative, path in files.items()
    )


def workspace_changed_paths(workspace_id: str) -> list[str]:
    """返回 baseline 与 working 的完整字节级变化，而不是有界写入日志。"""

    baseline_files = _iter_snapshot_files(_snapshot_root(workspace_id, "baseline"))
    final_files = _iter_snapshot_files(_snapshot_root(workspace_id, "working"))
    _assert_no_protected_changes(baseline_files, final_files)
    return [
        relative
        for relative in sorted(set(baseline_files) | set(final_files))
        if (
            baseline_files[relative].read_bytes()
            if relative in baseline_files
            else None
        )
        != (final_files[relative].read_bytes() if relative in final_files else None)
    ]


def _hash_snapshot(snapshot_root: Path) -> dict[str, str]:
    return {
        relative: _sha256_bytes(path.read_bytes())
        for relative, path in sorted(_iter_snapshot_files(snapshot_root).items())
    }


def create_run_workspace() -> dict[str, Any]:
    """为一次状态机运行建立不可变基线与可写副本。

    baseline/working 双快照让聚合 diff 不依赖真实目录在长时间 HITL 期间保持不变；
    真正提交前仍会再次对真实文件做 optimistic concurrency check。
    """

    workspace_id = uuid.uuid4().hex
    workspace_dir = _safe_workspace_dir(workspace_id)
    baseline_root = workspace_dir / "baseline"
    working_root = workspace_dir / "working"
    WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)
    try:
        for source_path in REFACTOR_ROOT.rglob("*"):
            is_junction = getattr(source_path, "is_junction", lambda: False)
            if source_path.is_symlink() or is_junction():
                raise WorkspaceError("CodeSmells 包含符号链接或目录联接，拒绝越界复制")
        shutil.copytree(
            REFACTOR_ROOT,
            baseline_root / "CodeSmells",
            ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc"),
        )
        shutil.copytree(
            baseline_root / "CodeSmells",
            working_root / "CodeSmells",
        )
        baseline_hashes = _hash_snapshot(baseline_root)
    except Exception:
        if workspace_dir.exists():
            shutil.rmtree(workspace_dir)
        raise
    return {
        "workspace_id": workspace_id,
        "baseline_file_hashes": baseline_hashes,
        "final_file_hashes": {},
        "final_workspace_snapshot_digest": "",
        "aggregate_diff": "",
        "workspace_approved": False,
        "workspace_applied": False,
        "workspace_rolled_back": False,
        "workspace_cleaned": False,
        "workspace_error": None,
    }


def initialize_workspace_node(_state: State) -> dict[str, Any]:
    """START 后立即创建 run 级隔离资源，使所有角色共享同一可恢复快照。"""

    return create_run_workspace()


def build_aggregate_diff(
    workspace_id: str,
) -> tuple[str, dict[str, str], list[str]]:
    baseline_root = _snapshot_root(workspace_id, "baseline")
    working_root = _snapshot_root(workspace_id, "working")
    baseline_files = _iter_snapshot_files(baseline_root)
    final_files = _iter_snapshot_files(working_root)
    _assert_no_protected_changes(baseline_files, final_files)
    final_hashes = {
        relative: _sha256_bytes(path.read_bytes())
        for relative, path in sorted(final_files.items())
    }
    changed_files: list[str] = []
    diff_sections: list[str] = []
    for relative in sorted(set(baseline_files) | set(final_files)):
        before_bytes = (
            baseline_files[relative].read_bytes()
            if relative in baseline_files
            else None
        )
        after_bytes = (
            final_files[relative].read_bytes() if relative in final_files else None
        )
        if before_bytes == after_bytes:
            continue
        changed_files.append(relative)
        before = before_bytes.decode("utf-8") if before_bytes is not None else ""
        after = after_bytes.decode("utf-8") if after_bytes is not None else ""
        file_diff = list(
            difflib.unified_diff(
                before.splitlines(),
                after.splitlines(),
                fromfile=f"a/{relative}",
                tofile=f"b/{relative}",
                lineterm="",
            )
        )
        if not file_diff:
            # unified_diff 会忽略纯换行风格变化和空文件创建/删除。若不显式展示，
            # 原子提交可能包含用户在聚合审批中看不到的字节级变更。
            change_kind = (
                "创建空文件"
                if before_bytes is None
                else (
                    "删除空文件"
                    if after_bytes is None
                    else "仅换行符或文件结尾发生变化"
                )
            )
            file_diff = [
                f"--- a/{relative}",
                f"+++ b/{relative}",
                "@@ 字节级变更 @@",
                f"- {change_kind}",
                f"+ before_sha256={_sha256_bytes(before_bytes or b'')}",
                f"+ after_sha256={_sha256_bytes(after_bytes or b'')}",
            ]
        diff_sections.extend(file_diff)
    aggregate_diff = "\n".join(diff_sections)
    if len(aggregate_diff) > MAX_AGGREGATE_DIFF_CHARS:
        raise WorkspaceError(
            "聚合 diff 超出安全展示上限，拒绝在用户无法完整审查时应用变更"
        )
    if not changed_files:
        raise WorkspaceError("隔离工作区没有可供审批的最终变更")
    return aggregate_diff, final_hashes, changed_files


def prepare_workspace_approval_node(state: State) -> dict[str, Any]:
    workspace_id = state.get("workspace_id")
    if not workspace_id:
        return {
            "review_status": "failed",
            "workspace_error": "运行缺少隔离工作区",
        }
    try:
        from .state import review_evidence_errors

        evidence_errors = review_evidence_errors(state)
        if evidence_errors:
            raise WorkspaceError(
                "最终审批前测试证据失效: " + "；".join(evidence_errors)
            )
        aggregate_diff, final_hashes, changed_files = build_aggregate_diff(workspace_id)
        final_snapshot = build_workspace_snapshot(workspace_id)
        return {
            "aggregate_diff": aggregate_diff,
            "final_file_hashes": final_hashes,
            "final_workspace_snapshot_digest": final_snapshot["digest"],
            "workspace_changed_files": changed_files,
            "workspace_error": None,
        }
    except Exception as exc:
        cleanup_run_workspace(workspace_id)
        return {
            "review_status": "failed",
            "workspace_error": f"生成最终聚合 diff 失败: {exc}",
            "workspace_cleaned": True,
        }


def route_workspace_preparation(state: State) -> str:
    """只有完整 diff 与哈希均已固化时才允许进入最终审批。"""

    return "cleanup_workspace" if state.get("workspace_error") else "apply_workspace"


def _current_hash(path: Path) -> str | None:
    return _sha256_bytes(path.read_bytes()) if path.is_file() else None


def _ensure_parent_directory(path: Path, created_dirs: list[Path]) -> None:
    missing: list[Path] = []
    current = path.parent
    while not current.exists():
        if not current.is_relative_to(REFACTOR_ROOT):
            raise WorkspaceError("目标父目录越出 CodeSmells")
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        directory.mkdir()
        created_dirs.append(directory)


def _write_same_directory_temp(target: Path, content: bytes, marker: str) -> Path:
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{target.name}.{marker}-",
        dir=target.parent,
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        Path(temp_name).unlink(missing_ok=True)
        raise
    return Path(temp_name)


def atomic_apply_workspace(state: State) -> None:
    """以 prepare/commit/compensate 三段式事务应用全部文件。

    文件系统没有跨文件原生事务，因此先在每个目标的同目录准备新文件与备份，
    再用 Windows/POSIX 都支持的 ``os.replace`` 提交。任一步失败会逆序执行补偿，
    这对应 Saga 的 compensation，但在本地临界区内对外提供“全有或全无”语义。
    """

    workspace_id = state.get("workspace_id")
    if not workspace_id:
        raise WorkspaceError("运行缺少隔离工作区")
    baseline_root = _snapshot_root(workspace_id, "baseline")
    working_root = _snapshot_root(workspace_id, "working")
    if _hash_snapshot(baseline_root) != state.get("baseline_file_hashes", {}):
        raise WorkspaceError("隔离工作区基线已被篡改，拒绝应用")
    if _hash_snapshot(working_root) != state.get("final_file_hashes", {}):
        raise WorkspaceError("审批后的隔离工作区内容已改变，拒绝应用")
    baseline_files = _iter_snapshot_files(baseline_root)
    final_files = _iter_snapshot_files(working_root)
    _assert_no_protected_changes(baseline_files, final_files)
    current_snapshot = build_workspace_snapshot(workspace_id)
    review_evidence = state.get("review_evidence")
    reviewed_digest = (
        review_evidence.get("workspace_snapshot_digest")
        if review_evidence is not None
        else None
    )
    if not reviewed_digest or current_snapshot["digest"] != reviewed_digest:
        raise WorkspaceError("当前工作区已偏离 Reviewer 成功测试快照，拒绝应用")
    if current_snapshot["digest"] != state.get("final_workspace_snapshot_digest", ""):
        raise WorkspaceError("审批后的完整工作区快照已改变，拒绝应用")
    changed = [
        relative
        for relative in sorted(set(baseline_files) | set(final_files))
        if (
            baseline_files[relative].read_bytes()
            if relative in baseline_files
            else None
        )
        != (final_files[relative].read_bytes() if relative in final_files else None)
    ]
    recorded_hashes = state.get("baseline_file_hashes", {})
    for relative in changed:
        target = (PROJECT_ROOT / relative).resolve(strict=False)
        if not target.is_relative_to(REFACTOR_ROOT):
            raise WorkspaceError("原子应用目标越出 CodeSmells")
        expected_hash = recorded_hashes.get(relative)
        if _current_hash(target) != expected_hash:
            raise BaselineConflictError(
                f"基线冲突: `{relative}` 已被外部修改，拒绝覆盖"
            )

    prepared: dict[str, Path] = {}
    backups: dict[str, Path] = {}
    created_dirs: list[Path] = []
    applied: list[str] = []
    try:
        # Prepare 阶段不触碰目标文件内容；全部临时文件关闭后才进入 replace，
        # 避免 Windows 上仍被打开的 NamedTemporaryFile 导致 PermissionError。
        for relative in changed:
            target = (PROJECT_ROOT / relative).resolve(strict=False)
            _ensure_parent_directory(target, created_dirs)
            if relative in final_files:
                prepared[relative] = _write_same_directory_temp(
                    target, final_files[relative].read_bytes(), "prepared"
                )
            if target.exists():
                backups[relative] = _write_same_directory_temp(
                    target, target.read_bytes(), "backup"
                )

        for relative in changed:
            target = (PROJECT_ROOT / relative).resolve(strict=False)
            if relative in final_files:
                os.replace(prepared[relative], target)
            else:
                os.replace(target, backups[relative])
            applied.append(relative)
    except Exception as apply_error:
        rollback_errors: list[str] = []
        for relative in reversed(applied):
            target = (PROJECT_ROOT / relative).resolve(strict=False)
            try:
                backup = backups.get(relative)
                if backup and backup.exists():
                    os.replace(backup, target)
                else:
                    target.unlink(missing_ok=True)
            except Exception as rollback_error:
                rollback_errors.append(
                    f"{relative}: {rollback_error.__class__.__name__}"
                )
        for directory in reversed(created_dirs):
            try:
                directory.rmdir()
            except OSError:
                pass
        if rollback_errors:
            raise AtomicApplyError(
                "原子应用失败，且补偿回滚不完整: " + "；".join(rollback_errors),
                rolled_back=False,
            ) from apply_error
        raise AtomicApplyError(
            f"原子应用失败，已完整回滚: {apply_error}",
            rolled_back=bool(applied),
        ) from apply_error
    finally:
        for temp_path in [*prepared.values(), *backups.values()]:
            temp_path.unlink(missing_ok=True)


def cleanup_run_workspace(workspace_id: str) -> None:
    workspace_dir = _safe_workspace_dir(workspace_id)
    if workspace_dir.exists():
        shutil.rmtree(workspace_dir)


def cleanup_workspace_node(state: State) -> dict[str, Any]:
    workspace_id = state.get("workspace_id")
    if not workspace_id:
        return {"workspace_cleaned": True}
    try:
        cleanup_run_workspace(workspace_id)
        return {"workspace_cleaned": True}
    except Exception as exc:
        return {
            "workspace_cleaned": False,
            "workspace_error": f"清理隔离工作区失败: {exc}",
        }


def apply_workspace_changes_node(state: State) -> dict[str, Any]:
    changed_files = state.get("workspace_changed_files", [])
    try:
        approval_result = interrupt(
            {
                "type": "aggregate_diff_approval",
                "file_path": f"聚合变更（{len(changed_files)} 个文件）",
                "original_code": "",
                "refactored_code": state.get("aggregate_diff", ""),
                "aggregate_diff": state.get("aggregate_diff", ""),
                "changed_files": changed_files,
                "workspace_id": state.get("workspace_id"),
            }
        )
        approved = (
            approval_result
            if isinstance(approval_result, bool)
            else (
                bool(approval_result.get("approved", False))
                if isinstance(approval_result, dict)
                else False
            )
        )
        if not approved:
            cleanup = cleanup_workspace_node(state)
            return {
                **cleanup,
                "review_status": "failed",
                "workspace_approved": False,
                "workspace_applied": False,
                "workspace_rolled_back": False,
                "workspace_error": "用户拒绝最终聚合 diff",
            }

        atomic_apply_workspace(state)
        # 索引刷新发生在真实文件事务提交之后。它仍保留 Neo4j 不可用时的 AST
        # 降级语义；索引失败不反向篡改已经由用户批准且成功提交的源码事务。
        try:
            from code_indexer import index_directory
            from graph_indexer import index_to_neo4j

            index_directory(REFACTOR_ROOT)
            index_to_neo4j()
        except Exception:
            pass
        cleanup = cleanup_workspace_node(state)
        return {
            **cleanup,
            "review_status": "success",
            "workspace_approved": True,
            "workspace_applied": True,
            "workspace_rolled_back": False,
            "workspace_error": cleanup.get("workspace_error"),
        }
    except GraphInterrupt:
        raise
    except Exception as exc:
        cleanup = cleanup_workspace_node(state)
        return {
            **cleanup,
            "review_status": "failed",
            "workspace_approved": True,
            "workspace_applied": False,
            "workspace_rolled_back": (
                exc.rolled_back if isinstance(exc, AtomicApplyError) else False
            ),
            "workspace_error": str(exc),
        }


def cleanup_all_workspaces() -> None:
    """正常进程退出时回收未完成 run；每个候选仍经过同一越界校验。"""

    if not WORKSPACE_ROOT.is_dir():
        return
    for candidate in WORKSPACE_ROOT.iterdir():
        if candidate.is_dir() and _WORKSPACE_ID_PATTERN.fullmatch(candidate.name):
            try:
                cleanup_run_workspace(candidate.name)
            except Exception:
                pass


atexit.register(cleanup_all_workspaces)
