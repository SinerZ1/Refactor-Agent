import sys
from pathlib import Path

# 历史样例目前仍使用顶层模块导入（例如 services -> models）。测试只在收集期
# 暴露 CodeSmells 根目录，不要求生产代码为测试改造成特定包结构。
CODE_SMELLS_ROOT = Path(__file__).resolve().parents[1]
if str(CODE_SMELLS_ROOT) not in sys.path:
    sys.path.insert(0, str(CODE_SMELLS_ROOT))
