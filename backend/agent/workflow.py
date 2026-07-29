import os
from urllib.parse import urlsplit, urlunsplit

from dotenv import load_dotenv
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

# 确保在工作流定义与 checkpointer 初始化前加载环境变量
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

# 构建状态图
workflow = StateGraph(State)

# 注册所有执行节点
workflow.add_node("architect", call_architect)
workflow.add_node("developer", call_developer)
workflow.add_node("reviewer", call_reviewer)
workflow.add_node("schedule_task", schedule_next_task_node)
workflow.add_node("complete_task", complete_active_task_node)
workflow.add_node("developer_retry", developer_retry_node)
workflow.add_node("reviewer_protocol_retry", reviewer_protocol_retry_node)
workflow.add_node("finalize_budget_failure", finalize_budget_failure_node)
workflow.add_node("finalize_plan_failure", finalize_plan_failure_node)
workflow.add_node("finalize_review_success", finalize_review_success_node)
workflow.add_node("finalize_review_failure", finalize_review_failure_node)
# LangGraph 1.0 的 StateGraph 泛型存根会把新增的精确 ``State -> dict`` 节点
# 误推断为 ``Never``；运行时仍由已声明的 State schema 校验输入输出。
workflow.add_node(
    "initialize_workspace",
    initialize_workspace_node,  # type: ignore[arg-type]
)
workflow.add_node("prepare_workspace_approval", prepare_workspace_approval_node)
workflow.add_node("apply_workspace", apply_workspace_changes_node)
workflow.add_node("cleanup_workspace", cleanup_workspace_node)

# 注册绑定的工具节点 (ToolNode)
workflow.add_node("architect_tools", ToolNode(architect_tools))
workflow.add_node("developer_tools", ToolNode(developer_tools))
workflow.add_node("reviewer_tools", ToolNode(reviewer_tools))

# ============================================================
# 设置连线与条件边关系
# ============================================================
workflow.add_edge(START, "initialize_workspace")
workflow.add_edge("initialize_workspace", "architect")

# Architect 条件边路由
workflow.add_conditional_edges(
    "architect",
    route_architect,
    {
        "architect_tools": "architect_tools",
        "schedule_task": "schedule_task",
        "finalize_budget_failure": "finalize_budget_failure",
    },
)
workflow.add_edge("architect_tools", "architect")

workflow.add_conditional_edges(
    "schedule_task",
    route_task_scheduler,
    {
        "developer": "developer",
        "reviewer": "reviewer",
        "finalize_budget_failure": "finalize_budget_failure",
        "finalize_plan_failure": "finalize_plan_failure",
    },
)

# Developer 条件边路由
workflow.add_conditional_edges(
    "developer",
    route_developer,
    {
        "developer_tools": "developer_tools",
        "complete_task": "complete_task",
        "reviewer": "reviewer",
        "finalize_budget_failure": "finalize_budget_failure",
    },
)
workflow.add_edge("developer_tools", "developer")
workflow.add_edge("complete_task", "schedule_task")

# Reviewer 打回先重开指定任务及下游，再由同一调度器选择 ready task。
workflow.add_edge("developer_retry", "schedule_task")

# Reviewer 条件边路由
workflow.add_conditional_edges(
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
workflow.add_edge("reviewer_tools", "reviewer")
workflow.add_edge("reviewer_protocol_retry", "reviewer")
workflow.add_edge("finalize_budget_failure", "cleanup_workspace")
workflow.add_edge("finalize_plan_failure", "cleanup_workspace")
workflow.add_edge("finalize_review_success", "prepare_workspace_approval")
workflow.add_conditional_edges(
    "prepare_workspace_approval",
    route_workspace_preparation,
    {
        "apply_workspace": "apply_workspace",
        "cleanup_workspace": "cleanup_workspace",
    },
)
workflow.add_edge("apply_workspace", END)
workflow.add_edge("finalize_review_failure", "cleanup_workspace")
workflow.add_edge("cleanup_workspace", END)


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


# 编译并导出状态图应用
app_graph = workflow.compile(checkpointer=get_checkpointer())
