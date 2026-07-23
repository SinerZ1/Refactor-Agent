from typing import Annotated, Literal, NotRequired, TypedDict

from langgraph.graph.message import add_messages

# ============================================================
# 教学说明: LangGraph Graph State 状态数据字典定义
# ------------------------------------------------------------
# 相当于 Hello-Agents 课程里讲过的 Context/Memory 存储协议，
# 在 LangGraph 中，我们通过 TypedDict 的 messages 属性以及 add_messages
# 实现追加/合并消息。当节点返回新的 messages 时，它们会被自动合并入全局状态。
# ============================================================


class State(TypedDict):
    messages: Annotated[list, add_messages]  # 合并/追加消息列表
    retry_count: int  # 记录 Reviewer 的重试次数
    review_protocol_errors: NotRequired[int]
    review_status: NotRequired[Literal["running", "success", "failed"]]
