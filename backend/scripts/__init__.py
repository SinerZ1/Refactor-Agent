import sys
import os
import subprocess

# 动态定位工作空间根目录
SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
BACKEND_DIR = os.path.dirname(SCRIPTS_DIR)
WORKSPACE_ROOT = os.path.dirname(BACKEND_DIR)

def run_command(args, run_in_root=True):
    """
    通用执行底层工具命令的方法。
    使用 sys.executable -m <tool> 形式调用，保证 100% 运行在当前虚拟环境的 Python 解释器中。
    通过 cwd 强制在项目根目录下执行，彻底解决相对路径问题。
    """
    cmd = [sys.executable, "-m"] + args
    cwd_dir = WORKSPACE_ROOT if run_in_root else BACKEND_DIR
    print(f"[Script Launcher] Executing command in {cwd_dir}: {' '.join(cmd)}")
    try:
        res = subprocess.run(cmd, cwd=cwd_dir)
        sys.exit(res.returncode)
    except Exception as e:
        print(f"[Script Launcher] Error executing command: {e}")
        sys.exit(1)

def check_format():
    run_command(["black", "--check", "CodeSmells", "backend", "--exclude", "backend/venv"])

def format():
    run_command(["black", "CodeSmells", "backend", "--exclude", "backend/venv"])

def check_sort_imports():
    run_command(["isort", "--check", "--profile", "black", "CodeSmells", "backend", "--skip", "backend/venv"])

def sort_imports():
    run_command(["isort", "--profile", "black", "CodeSmells", "backend", "--skip", "backend/venv"])

def check_mypy():
    run_command(["mypy", "CodeSmells", "backend/agent", "backend/app.py", "backend/code_indexer.py", "backend/graph_indexer.py", "backend/agent_core.py"])

def check_lint():
    run_command(["ruff", "check", "CodeSmells", "backend", "--exclude", "backend/venv"])

def find_dead_code():
    run_command(["vulture", "CodeSmells", "backend", "--exclude", "backend/venv"])

def test():
    run_command(["pytest"], run_in_root=False)

def test_verbose():
    run_command(["pytest", "-v"], run_in_root=False)

def test_coverage():
    run_command(["pytest", "--cov=backend"], run_in_root=False)
