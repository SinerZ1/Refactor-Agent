import json
from typing import Any, Literal, NotRequired, TypedDict

AgentEventType = Literal[
    "run.started",
    "run.completed",
    "run.failed",
    "run.retrying",
    "run.usage.updated",
    "run.budget.exceeded",
    "plan.created",
    "task.started",
    "task.completed",
    "tool.started",
    "tool.completed",
    "tool.failed",
    "approval.waiting",
    "approval.rejected",
    "review.passed",
    "review.failed",
    "agent.message.delta",
    "log",
]
AgentEventLevel = Literal["info", "success", "error"]


class AgentEvent(TypedDict):
    """前后端共享的版本化 Agent 事件信封。

    这相当于 Hello-Agents 中的 ToolResponse/Event Envelope：自然语言只负责展示，
    状态机语义由 ``type``、``node``、``tool`` 与 ``success`` 明确表达。保留
    ``token`` 是一次兼容迁移策略，旧版前端仍可显示日志，但新版前端不再解析文案。
    """

    version: Literal[1]
    type: AgentEventType
    level: AgentEventLevel
    message: str
    token: str
    node: NotRequired[str]
    task_id: NotRequired[str]
    tool: NotRequired[str]
    success: NotRequired[bool]
    payload: NotRequired[dict[str, Any]]


def _legacy_token(
    event_type: AgentEventType,
    level: AgentEventLevel,
    message: str,
    payload: dict[str, Any] | None,
) -> str:
    """仅供旧客户端展示；任何业务状态都不得反向依赖该文本。"""

    if event_type == "agent.message.delta":
        return message
    if event_type == "approval.waiting":
        return "[APPROVAL_REQUEST]" + json.dumps(payload or {}, ensure_ascii=False)
    prefix = {"info": "[INFO]", "success": "[SUCCESS]", "error": "[ERROR]"}[level]
    return f"{prefix} {message}\n"


def make_agent_event(
    event_type: AgentEventType,
    message: str,
    *,
    level: AgentEventLevel = "info",
    node: str | None = None,
    task_id: str | None = None,
    tool: str | None = None,
    success: bool | None = None,
    payload: dict[str, Any] | None = None,
) -> AgentEvent:
    """构造稳定且可 JSON 序列化的事件，集中维护协议兼容字段。"""

    event: AgentEvent = {
        "version": 1,
        "type": event_type,
        "level": level,
        "message": message,
        "token": _legacy_token(event_type, level, message, payload),
    }
    if node is not None:
        event["node"] = node
    if task_id is not None:
        event["task_id"] = task_id
    if tool is not None:
        event["tool"] = tool
    if success is not None:
        event["success"] = success
    if payload is not None:
        event["payload"] = payload
    return event
