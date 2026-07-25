from typing import Annotated, Any, Literal, NotRequired, TypedDict

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


def get_message_text(content: Any) -> str:
    """
    教学与理论关联 - 鲁棒的数据清洗与多模态内容提取 (Robust Data Sanitization for Multimodal Output)
    --------------------------------------------------------------------------------------
    在多智能体协同（A2A）与大模型 (LLM) 交互的实践中，由于不同 LLM 供应商 (如 Google Vertex AI,
    OpenAI 或 Anthropic) 的 API 响应结构差异，消息的主体内容 (message.content) 可能会被解析为：
    1. 传统的纯文本字符串 (str)。
    2. 多模态或富文本块列表 (list[dict])，例如包含文本块 `{"type": "text", "text": "..."}` 或工具调用块。
    
    为了在下游节点 (如 Reviewer 协议解析、流式打字机输出或 A2A 聊天室渲染) 中保持代码的鲁棒性 (Robustness)
    并防止“can only concatenate str (not 'list') to str”等类型拼接崩溃 (Type Interoperability Issues)，
    此处通过防御性编程 (Defensive Programming) 设计一个集中的数据降级与序列化提取器。
    这相当于 Hello-Agents 框架中所采用的统一消息信荷 (Payload) 归一化网关设计。
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if "text" in block:
                    parts.append(str(block["text"]))
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts)
    return str(content or "")
