import os
import sys
from pathlib import Path

# Reviewer 运行时显式注入 working/CodeSmells；普通开发测试则回退到仓库真实样例。
# 测试代码本身始终来自 backend/behavior_tests，只有被测模块搜索根会随 run 改变。
PROJECT_ROOT = Path(__file__).resolve().parents[2]
CODE_SMELLS_ROOT = Path(
    os.environ.get("REFACTOR_CODE_ROOT", PROJECT_ROOT / "CodeSmells")
).resolve(strict=True)
if CODE_SMELLS_ROOT.name.casefold() != "codesmells":
    raise RuntimeError("行为契约测试只能面向 CodeSmells 源码根目录")
sys.path.insert(0, str(CODE_SMELLS_ROOT))
