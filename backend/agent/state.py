from typing import Annotated, Literal, NotRequired, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages

# ============================================================
# 教学说明: LangGraph Graph State 状态数据字典定义
# ------------------------------------------------------------
# 相当于 Hello-Agents 课程里讲过的 Context/Memory 存储协议，
# 在 LangGraph 中，我们通过 TypedDict 的 messages 属性以及 add_messages
# 实现追加/合并消息。当节点返回新的 messages 时，它们会被自动合并入全局状态。
# ============================================================


MAX_CHANGE_RECORDS = 12


class ChangeRecord(TypedDict):
    """由写入工具生成、供 Reviewer 只读消费的可验证变更事实。"""

    file_path: str
    before_sha256: str
    after_sha256: str
    added_lines: int
    removed_lines: int
    unified_diff: str
    diff_truncated: bool


def merge_change_records(
    existing: list[ChangeRecord], updates: list[ChangeRecord]
) -> list[ChangeRecord]:
    """追加变更事实并限制 Checkpoint 体积。

    这里没有覆盖旧记录，因为 Reviewer 需要看到多文件写入和失败重试的演进过程；
    同时只保留最近若干次写入，避免长会话把 Redis/MemorySaver 快照无限放大。
    """

    return (existing + updates)[-MAX_CHANGE_RECORDS:]


class State(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]  # 合并/追加消息列表
    retry_count: int  # 记录 Reviewer 的重试次数
    review_protocol_errors: NotRequired[int]
    review_status: NotRequired[Literal["running", "success", "failed"]]
    change_records: NotRequired[Annotated[list[ChangeRecord], merge_change_records]]
