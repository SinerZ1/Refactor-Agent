from collections.abc import Mapping
from typing import Any, TypedDict, cast

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig

DEFAULT_MAX_AGENT_STEPS = 24
DEFAULT_MAX_TOOL_CALLS = 32
DEFAULT_MAX_TOTAL_TOKENS = 120_000
DEFAULT_MODEL_TIMEOUT_SECONDS = 60


class RunBudgetLimits(TypedDict):
    max_agent_steps: int
    max_tool_calls: int
    max_total_tokens: int
    model_timeout_seconds: int


class RunUsage(TypedDict):
    agent_steps: int
    tool_calls: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    unmetered_steps: int


def empty_run_usage() -> RunUsage:
    return {
        "agent_steps": 0,
        "tool_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "unmetered_steps": 0,
    }


def _bounded_int(
    value: object,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    """直接调用 Graph 时仍需防御配置污染，API 的 Pydantic 校验不是唯一入口。"""

    if isinstance(value, bool):
        return default
    try:
        parsed = int(cast(Any, value))
    except (TypeError, ValueError):
        return default
    return min(max(parsed, minimum), maximum)


def get_run_budget_limits(config: RunnableConfig | None) -> RunBudgetLimits:
    configurable = config.get("configurable", {}) if config else {}
    raw_budget = configurable.get("run_budget", {})
    budget = raw_budget if isinstance(raw_budget, Mapping) else {}
    return {
        "max_agent_steps": _bounded_int(
            budget.get("max_agent_steps"),
            default=DEFAULT_MAX_AGENT_STEPS,
            minimum=3,
            maximum=100,
        ),
        "max_tool_calls": _bounded_int(
            budget.get("max_tool_calls"),
            default=DEFAULT_MAX_TOOL_CALLS,
            minimum=1,
            maximum=200,
        ),
        "max_total_tokens": _bounded_int(
            budget.get("max_total_tokens"),
            default=DEFAULT_MAX_TOTAL_TOKENS,
            minimum=1_000,
            maximum=2_000_000,
        ),
        "model_timeout_seconds": _bounded_int(
            budget.get("model_timeout_seconds"),
            default=DEFAULT_MODEL_TIMEOUT_SECONDS,
            minimum=5,
            maximum=300,
        ),
    }


def get_run_usage(state: Mapping[str, Any]) -> RunUsage:
    raw_usage = state.get("run_usage")
    usage = raw_usage if isinstance(raw_usage, Mapping) else {}
    return {
        "agent_steps": _bounded_int(
            usage.get("agent_steps"), default=0, minimum=0, maximum=10_000
        ),
        "tool_calls": _bounded_int(
            usage.get("tool_calls"), default=0, minimum=0, maximum=10_000
        ),
        "input_tokens": _bounded_int(
            usage.get("input_tokens"), default=0, minimum=0, maximum=100_000_000
        ),
        "output_tokens": _bounded_int(
            usage.get("output_tokens"), default=0, minimum=0, maximum=100_000_000
        ),
        "total_tokens": _bounded_int(
            usage.get("total_tokens"), default=0, minimum=0, maximum=100_000_000
        ),
        "unmetered_steps": _bounded_int(
            usage.get("unmetered_steps"), default=0, minimum=0, maximum=10_000
        ),
    }


def _nonnegative_token(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(int(cast(Any, value)), 0)
    except (TypeError, ValueError):
        return 0


def extract_token_usage(message: AIMessage) -> tuple[int, int, int, bool]:
    """归一化 OpenAI/Gemini 的 usage 字段，并显式报告是否真正可计量。

    Token 预算依赖供应商返回的计量事实，不能通过字符数伪装成精确 Token。自建
    OpenAI-compatible 服务若省略 usage，会增加 ``unmetered_steps``；此时 Agent
    步数与工具调用预算仍提供确定性的循环上限。
    """

    usage = message.usage_metadata
    usage_keys = {"input_tokens", "output_tokens", "total_tokens"}
    if isinstance(usage, Mapping) and usage_keys.intersection(usage):
        input_tokens = _nonnegative_token(usage.get("input_tokens"))
        output_tokens = _nonnegative_token(usage.get("output_tokens"))
        total_tokens = _nonnegative_token(usage.get("total_tokens"))
        return (
            input_tokens,
            output_tokens,
            max(total_tokens, input_tokens + output_tokens),
            True,
        )

    response_metadata = message.response_metadata
    raw_fallback = (
        response_metadata.get("token_usage")
        or response_metadata.get("usage_metadata")
        or response_metadata.get("usage")
    )
    fallback_keys = {
        "input_tokens",
        "prompt_tokens",
        "output_tokens",
        "completion_tokens",
        "total_tokens",
    }
    if isinstance(raw_fallback, Mapping) and fallback_keys.intersection(raw_fallback):
        input_tokens = _nonnegative_token(
            raw_fallback.get("input_tokens") or raw_fallback.get("prompt_tokens")
        )
        output_tokens = _nonnegative_token(
            raw_fallback.get("output_tokens") or raw_fallback.get("completion_tokens")
        )
        total_tokens = _nonnegative_token(raw_fallback.get("total_tokens"))
        return (
            input_tokens,
            output_tokens,
            max(total_tokens, input_tokens + output_tokens),
            True,
        )
    return 0, 0, 0, False


def budget_preflight_reason(
    state: Mapping[str, Any],
    config: RunnableConfig | None,
) -> str | None:
    usage = get_run_usage(state)
    limits = get_run_budget_limits(config)
    if usage["agent_steps"] >= limits["max_agent_steps"]:
        return (
            f"Agent 步数已达到上限 {limits['max_agent_steps']}，"
            "为避免工作流循环已停止下一次模型调用"
        )
    if usage["total_tokens"] >= limits["max_total_tokens"]:
        return f"累计 Token 已达到上限 {limits['max_total_tokens']}"
    return None


def account_agent_response(
    state: Mapping[str, Any],
    config: RunnableConfig | None,
    response: AIMessage,
) -> tuple[AIMessage, RunUsage, RunBudgetLimits, str | None]:
    """记录一次模型响应，并在执行其工具意图前进行预算熔断。

    预算超限时会把带 tool_calls 的响应替换为普通 AIMessage。这样 LangGraph 不会留下
    “有 ToolCall 却没有 ToolResponse”的非法对话尾部，也保证超额写文件意图不会进入
    HITL。该决策相当于 Agent 控制面的 circuit breaker，而不是 Prompt 层软提醒。
    """

    limits = get_run_budget_limits(config)
    current = get_run_usage(state)
    input_tokens, output_tokens, total_tokens, metered = extract_token_usage(response)
    usage: RunUsage = {
        "agent_steps": current["agent_steps"] + 1,
        "tool_calls": current["tool_calls"] + len(response.tool_calls),
        "input_tokens": current["input_tokens"] + input_tokens,
        "output_tokens": current["output_tokens"] + output_tokens,
        "total_tokens": current["total_tokens"] + total_tokens,
        "unmetered_steps": current["unmetered_steps"] + (0 if metered else 1),
    }

    reason: str | None = None
    if usage["tool_calls"] > limits["max_tool_calls"]:
        reason = (
            f"本次响应会使工具调用数达到 {usage['tool_calls']}，"
            f"超过上限 {limits['max_tool_calls']}"
        )
    elif usage["total_tokens"] > limits["max_total_tokens"]:
        reason = (
            f"累计 Token 达到 {usage['total_tokens']}，"
            f"超过上限 {limits['max_total_tokens']}"
        )
    elif usage["agent_steps"] >= limits["max_agent_steps"] and response.tool_calls:
        reason = (
            f"Agent 步数已达到上限 {limits['max_agent_steps']}，"
            "当前工具意图需要额外模型回合才能闭环"
        )

    if reason is not None:
        disposition = (
            "未执行本次工具调用。"
            if response.tool_calls
            else "本次响应未进入后续工作流。"
        )
        response = AIMessage(
            content=f"【RUN_BUDGET_EXCEEDED】{reason}。{disposition}",
            response_metadata=response.response_metadata,
            usage_metadata=response.usage_metadata,
        )
    return response, usage, limits, reason
