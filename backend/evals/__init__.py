"""Refactor-Agent 的可复现评测入口。

``offline`` 模式只使用仓库内脚本化 fake model；``live`` 模式必须由调用者显式选择，
并复用生产模型适配器。评测报告只保留归一化结果，原始模型响应不会落盘。
"""

from .runner import EvaluationRunner

__all__ = ["EvaluationRunner"]
