import os
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from dotenv import load_dotenv
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from .edges import (
    route_architect,
    route_developer,
    route_reviewer,
    route_task_scheduler,
)
from .nodes import (
    call_architect,
    call_developer,
    call_reviewer,
    developer_retry_node,
    finalize_budget_failure_node,
    finalize_plan_failure_node,
    finalize_review_failure_node,
    finalize_review_success_node,
    get_llm_from_config,
    reviewer_protocol_retry_node,
)
from .scheduler import complete_active_task_node, schedule_next_task_node

# 使用高内聚相对导入，解耦子包结构
from .state import State
from .tools import architect_tools, developer_tools, reviewer_tools
from .workspace import (
    apply_workspace_changes_node,
    cleanup_workspace_node,
    initialize_workspace_node,
    prepare_workspace_approval_node,
    route_workspace_preparation,
)

# 离线 Eval 在导入生产图工厂时显式禁止读取 .env 与探测基础设施。生产服务仍沿用
# 既有环境加载语义；该进程级开关只在 Eval 模块导入的窄窗口存在。
if os.getenv("REFACTOR_OFFLINE_EVAL_IMPORT") != "1":
    load_dotenv()

_checkpointer_health = {
    "status": "degraded",
    "backend": "memory",
    "reason": "not_configured",
}

# ============================================================
# 教学说明: 状态图装配层 (StateGraph Assembly) 与持久化 Checkpointer
# ------------------------------------------------------------
# 这里在底层等同于 Hello-Agents 工作流编排。
# 我们通过 StateGraph 定义节点和边，并绑定持久化 Checkpointer。
# Checkpointer 保证了状态机在每次中断（如 HITL 审批或 WebSocket 挂起）时，
# 都会把当前的 Graph State 快照（Checkpoint）落地保存，支持异步精准唤醒。
# ============================================================


def build_agent_graph(
    *,
    checkpointer: Any,
    model_resolver: Callable[[Any], Any] | None = None,
):
    """从同一生产装配函数编译可注入依赖的 Agent 图。

    Eval 只替换外部模型与 Checkpointer；节点、ToolNode、scheduler、条件边、HITL
    与工作区事务仍由这里唯一装配。这样 scripted model 是生产控制面的输入端口，
    而不是一套能读取预期答案的平行模拟器。
    """

    graph = StateGraph(State)

    resolver = model_resolver or get_llm_from_config

    # 闭包只替换模型解析端口，不改变节点函数内部的 Prompt、预算或工具绑定。
    def architect_with_model(state: State, config: RunnableConfig):
        return call_architect(state, config, model_resolver=resolver)

    def developer_with_model(state: State, config: RunnableConfig):
        return call_developer(state, config, model_resolver=resolver)

    def reviewer_with_model(state: State, config: RunnableConfig):
        return call_reviewer(state, config, model_resolver=resolver)

    graph.add_node("architect", architect_with_model)
    graph.add_node("developer", developer_with_model)
    graph.add_node("reviewer", reviewer_with_model)
    graph.add_node("schedule_task", schedule_next_task_node)
    graph.add_node("complete_task", complete_active_task_node)
    graph.add_node("developer_retry", developer_retry_node)
    graph.add_node("reviewer_protocol_retry", reviewer_protocol_retry_node)
    graph.add_node("finalize_budget_failure", finalize_budget_failure_node)
    graph.add_node("finalize_plan_failure", finalize_plan_failure_node)
    graph.add_node("finalize_review_success", finalize_review_success_node)
    graph.add_node("finalize_review_failure", finalize_review_failure_node)
    graph.add_node(
        "initialize_workspace",
        initialize_workspace_node,  # type: ignore[arg-type]
    )
    graph.add_node("prepare_workspace_approval", prepare_workspace_approval_node)
    graph.add_node("apply_workspace", apply_workspace_changes_node)
    graph.add_node("cleanup_workspace", cleanup_workspace_node)

    graph.add_node("architect_tools", ToolNode(architect_tools))
    graph.add_node("developer_tools", ToolNode(developer_tools))
    graph.add_node("reviewer_tools", ToolNode(reviewer_tools))

    graph.add_edge(START, "initialize_workspace")
    graph.add_edge("initialize_workspace", "architect")
    graph.add_conditional_edges(
        "architect",
        route_architect,
        {
            "architect_tools": "architect_tools",
            "schedule_task": "schedule_task",
            "finalize_budget_failure": "finalize_budget_failure",
        },
    )
    graph.add_edge("architect_tools", "architect")
    graph.add_conditional_edges(
        "schedule_task",
        route_task_scheduler,
        {
            "developer": "developer",
            "reviewer": "reviewer",
            "finalize_budget_failure": "finalize_budget_failure",
            "finalize_plan_failure": "finalize_plan_failure",
        },
    )
    graph.add_conditional_edges(
        "developer",
        route_developer,
        {
            "developer_tools": "developer_tools",
            "complete_task": "complete_task",
            "reviewer": "reviewer",
            "finalize_budget_failure": "finalize_budget_failure",
        },
    )
    graph.add_edge("developer_tools", "developer")
    graph.add_edge("complete_task", "schedule_task")
    graph.add_edge("developer_retry", "schedule_task")
    graph.add_conditional_edges(
        "reviewer",
        route_reviewer,
        {
            "reviewer_tools": "reviewer_tools",
            "developer_retry": "developer_retry",
            "reviewer_protocol_retry": "reviewer_protocol_retry",
            "finalize_budget_failure": "finalize_budget_failure",
            "finalize_review_success": "finalize_review_success",
            "finalize_review_failure": "finalize_review_failure",
        },
    )
    graph.add_edge("reviewer_tools", "reviewer")
    graph.add_edge("reviewer_protocol_retry", "reviewer")
    graph.add_edge("finalize_budget_failure", "cleanup_workspace")
    graph.add_edge("finalize_plan_failure", "cleanup_workspace")
    graph.add_edge("finalize_review_success", "prepare_workspace_approval")
    graph.add_conditional_edges(
        "prepare_workspace_approval",
        route_workspace_preparation,
        {
            "apply_workspace": "apply_workspace",
            "cleanup_workspace": "cleanup_workspace",
        },
    )
    graph.add_edge("apply_workspace", END)
    graph.add_edge("finalize_review_failure", "cleanup_workspace")
    graph.add_edge("cleanup_workspace", END)
    return graph.compile(checkpointer=checkpointer)


def _safe_connection_url(connection_url: str) -> str:
    """仅保留定位服务所需的信息，用户名、口令和查询参数永不进入日志。"""

    try:
        parsed = urlsplit(connection_url)
        hostname = parsed.hostname or "<unknown>"
        if ":" in hostname:
            hostname = f"[{hostname}]"
        netloc = hostname
        if parsed.port is not None:
            netloc = f"{netloc}:{parsed.port}"
        return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))
    except ValueError:
        return "<invalid Redis URL>"


def get_checkpointer_health() -> dict[str, str]:
    """返回副本，避免健康检查调用方覆写进程级组件状态。"""

    return dict(_checkpointer_health)


def get_checkpointer():
    """
    持久化记忆存储器适配方法。
    优先读取 Redis，降级至全内存方案。
    """
    redis_url = os.getenv("REDIS_URL")
    if redis_url:
        # 防御性兼容：若用户误配置为 http:// 或 https://，自动修正为 redis:// 或 rediss://
        if redis_url.startswith("http://"):
            redis_url = redis_url.replace("http://", "redis://", 1)
            print(
                "[Warning] 检测到 REDIS_URL 使用了错误协议头 http://，"
                f"已防御性自动修正为: {_safe_connection_url(redis_url)}"
            )
        elif redis_url.startswith("https://"):
            redis_url = redis_url.replace("https://", "rediss://", 1)
            print(
                "[Warning] 检测到 REDIS_URL 使用了错误协议头 https://，"
                f"已防御性自动修正为: {_safe_connection_url(redis_url)}"
            )

        try:
            import redis

            client = redis.Redis.from_url(redis_url, socket_timeout=3.0)
            client.ping()

            from langgraph.checkpoint.redis import RedisSaver

            saver = RedisSaver(redis_url=redis_url, redis_client=client)
            if hasattr(saver, "setup") and callable(getattr(saver, "setup")):
                saver.setup()
            print(
                "[Checkpointer] Redis 连接与 setup 成功。成功加载 RedisSaver 持久化记忆。"
            )
            _checkpointer_health.update(
                status="ok", backend="redis", reason="available"
            )
            return saver
        except Exception as exc:
            _checkpointer_health.update(
                status="degraded",
                backend="memory",
                reason=f"redis_unavailable:{exc.__class__.__name__}",
            )
            print(
                "[Checkpointer] Redis 连接或初始化失败 "
                f"(URL: {_safe_connection_url(redis_url)}, "
                f"错误类型: {exc.__class__.__name__})。将降级使用 MemorySaver。"
            )
    else:
        _checkpointer_health.update(
            status="degraded", backend="memory", reason="not_configured"
        )
        print("[Checkpointer] 未配置 REDIS_URL，当前使用的是 MemorySaver 记忆方案。")
    return MemorySaver()


# 编译并导出状态图应用。生产与 Eval 都从同一个图工厂创建实例。离线导入不能读取
# REDIS_URL 或发起 ping；场景本身随后各自创建独立 MemorySaver。
app_graph = build_agent_graph(
    checkpointer=(
        MemorySaver()
        if os.getenv("REFACTOR_OFFLINE_EVAL_IMPORT") == "1"
        else get_checkpointer()
    )
)
