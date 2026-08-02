from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
REFACTOR_ROOT = (PROJECT_ROOT / "CodeSmells").resolve()

# 可信行为契约已经迁出 Agent 可写区；旧位置仍永久保留为受保护别名，防止模型
# 通过“重新创建 CodeSmells/tests”把伪造断言混入候选变更。这里使用目录身份而非
# 文件名黑名单，因此新增、修改、删除与重命名最终都会落到同一条策略上。
PROTECTED_PATH_PREFIXES = (
    "backend/behavior_tests",
    "codesmells/tests",
)


class PathPolicyError(ValueError):
    """不可信路径违反仓库能力边界。"""


def canonical_repository_path(
    file_path: str,
    *,
    project_root: Path = PROJECT_ROOT,
) -> str:
    """将 Windows/POSIX、绝对/相对输入归一化为仓库相对 POSIX 路径。

    ``Path.resolve`` 先折叠 ``.``、``..`` 和已有符号链接；随后再检查仓库归属。
    输出只用于身份比较，不用于绕过后续真实路径授权。Windows 文件系统不区分
    大小写，因此所有安全比较都对规范路径使用 ``casefold``。
    """

    if not file_path or not file_path.strip():
        raise PathPolicyError("文件路径不能为空")
    root = project_root.resolve(strict=False)
    requested = Path(file_path.strip().replace("\\", "/"))
    resolved = (
        requested.resolve(strict=False)
        if requested.is_absolute()
        else (root / requested).resolve(strict=False)
    )
    if not resolved.is_relative_to(root):
        raise PathPolicyError("文件路径超出仓库范围")
    return resolved.relative_to(root).as_posix()


def is_protected_path(
    file_path: str,
    *,
    project_root: Path = PROJECT_ROOT,
) -> bool:
    """判断路径是否属于可信测试边界；目录本身及全部后代都受保护。"""

    identity = canonical_repository_path(
        file_path,
        project_root=project_root,
    ).casefold()
    return any(
        identity == prefix or identity.startswith(f"{prefix}/")
        for prefix in PROTECTED_PATH_PREFIXES
    )


def canonical_refactor_path(
    file_path: str,
    *,
    project_root: Path = PROJECT_ROOT,
    refactor_root: Path = REFACTOR_ROOT,
) -> str:
    """规范化 Agent 源码路径，并拒绝可信测试及目录逃逸。"""

    if not file_path or not file_path.strip():
        raise PathPolicyError("文件路径不能为空")
    requested = Path(file_path.strip().replace("\\", "/"))
    if requested.is_absolute():
        raise PathPolicyError("仅允许使用 CodeSmells 目录内的相对路径")
    root = project_root.resolve(strict=False)
    allowed_root = refactor_root.resolve(strict=False)
    resolved = (root / requested).resolve(strict=False)
    if not resolved.is_relative_to(allowed_root):
        raise PathPolicyError("文件路径超出允许的 CodeSmells 重构工作区")
    normalized = resolved.relative_to(root).as_posix()
    if is_protected_path(normalized, project_root=root):
        raise PathPolicyError("行为契约测试属于可信只读边界，Agent 不得修改")
    return normalized
