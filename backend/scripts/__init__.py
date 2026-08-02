import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from typing import NoReturn

# 动态定位工作空间根目录
SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
BACKEND_DIR = os.path.dirname(SCRIPTS_DIR)
WORKSPACE_ROOT = os.path.dirname(BACKEND_DIR)


def run_command(
    args: Sequence[str],
    run_in_root: bool = True,
    env_overrides: Mapping[str, str] | None = None,
) -> NoReturn:
    """
    通用执行底层工具命令的方法。
    使用 sys.executable -m <tool> 形式调用，保证 100% 运行在当前虚拟环境的 Python 解释器中。
    通过 cwd 显式选择工作区或后端目录，保证工具配置发现和相对路径语义一致。
    """
    cmd = [sys.executable, "-m"] + args
    cwd_dir = WORKSPACE_ROOT if run_in_root else BACKEND_DIR
    command_env = os.environ.copy()
    command_env.update(env_overrides or {})
    print(f"[Script Launcher] Executing command in {cwd_dir}: {' '.join(cmd)}")
    try:
        res = subprocess.run(cmd, cwd=cwd_dir, env=command_env, check=False)
        sys.exit(res.returncode)
    except Exception as e:
        print(f"[Script Launcher] Error executing command: {e}")
        sys.exit(1)


def check_format():
    run_command(
        ["black", "--check", "../CodeSmells", "."],
        run_in_root=False,
        env_overrides={"BLACK_CACHE_DIR": os.path.join(BACKEND_DIR, ".cache", "black")},
    )


def format():
    run_command(
        ["black", "../CodeSmells", "."],
        run_in_root=False,
        env_overrides={"BLACK_CACHE_DIR": os.path.join(BACKEND_DIR, ".cache", "black")},
    )


def check_sort_imports():
    run_command(
        [
            "isort",
            "--check",
            "--profile",
            "black",
            "../CodeSmells",
            ".",
        ],
        run_in_root=False,
    )


def sort_imports():
    run_command(
        [
            "isort",
            "--profile",
            "black",
            "../CodeSmells",
            ".",
        ],
        run_in_root=False,
    )


def check_mypy():
    # CodeSmells 是教学用反例语料，故意保留动态全局状态等坏味道。类型门禁只覆盖
    # 可部署后端，避免为了让工具变绿而“修好”课程样本、削弱重构演示价值。
    run_command(
        [
            "mypy",
            "agent",
            "evals",
            "app.py",
            "code_indexer.py",
            "graph_indexer.py",
            "agent_core.py",
        ],
        run_in_root=False,
    )


def check_lint():
    run_command(["ruff", "check", "../CodeSmells", "."], run_in_root=False)


def find_dead_code():
    # FastAPI 路由、Pydantic 回调与 console script 入口包含框架隐式调用。回调中未使用
    # 的注入参数在定义处用下划线精确标记；这里仍保持 80% 高置信阈值和完整后端扫描，
    # 不以全局 ignore-name/ignore-decorator 掩盖同名的真实未使用符号。
    run_command(
        [
            "vulture",
            ".",
            "--exclude",
            "venv,.cache",
            "--min-confidence",
            "80",
        ],
        run_in_root=False,
    )


def test():
    run_command(["pytest"], run_in_root=False)


def test_verbose():
    run_command(["pytest", "-v"], run_in_root=False)


def test_coverage():
    run_command(["pytest", "--cov", "--cov-report=term-missing"], run_in_root=False)
