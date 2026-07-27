import os

from dotenv import load_dotenv
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from .edges import route_architect, route_developer, route_reviewer
from .nodes import (
    call_architect,
    call_developer,
    call_reviewer,
    developer_retry_node,
    finalize_budget_failure_node,
    finalize_review_failure_node,
    finalize_review_success_node,
    reviewer_protocol_retry_node,
)

# 使用高内聚相对导入，解耦子包结构
from .state import State
from .tools import architect_tools, developer_tools, reviewer_tools

# 确保在工作流定义与 checkpointer 初始化前加载环境变量
load_dotenv()

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
workflow.add_node("developer_retry", developer_retry_node)
workflow.add_node("reviewer_protocol_retry", reviewer_protocol_retry_node)
workflow.add_node("finalize_budget_failure", finalize_budget_failure_node)
workflow.add_node("finalize_review_success", finalize_review_success_node)
workflow.add_node("finalize_review_failure", finalize_review_failure_node)

# 注册绑定的工具节点 (ToolNode)
workflow.add_node("architect_tools", ToolNode(architect_tools))
workflow.add_node("developer_tools", ToolNode(developer_tools))
workflow.add_node("reviewer_tools", ToolNode(reviewer_tools))

# ============================================================
# 设置连线与条件边关系
# ============================================================
workflow.add_edge(START, "architect")

# Architect 条件边路由
workflow.add_conditional_edges(
    "architect",
    route_architect,
    {
        "architect_tools": "architect_tools",
        "developer": "developer",
        "finalize_budget_failure": "finalize_budget_failure",
    },
)
workflow.add_edge("architect_tools", "architect")

# Developer 条件边路由
workflow.add_conditional_edges(
    "developer",
    route_developer,
    {
        "developer_tools": "developer_tools",
        "reviewer": "reviewer",
        "finalize_budget_failure": "finalize_budget_failure",
    },
)
workflow.add_edge("developer_tools", "developer")

# Developer Retry 直连返回开发节点
workflow.add_edge("developer_retry", "developer")

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
workflow.add_edge("finalize_budget_failure", END)
workflow.add_edge("finalize_review_success", END)
workflow.add_edge("finalize_review_failure", END)


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
                f"[Warning] 检测到 REDIS_URL 使用了错误协议头 http://，已防御性自动修正为: {redis_url}"
            )
        elif redis_url.startswith("https://"):
            redis_url = redis_url.replace("https://", "rediss://", 1)
            print(
                f"[Warning] 检测到 REDIS_URL 使用了错误协议头 https://，已防御性自动修正为: {redis_url}"
            )

        try:
            import redis

            client = redis.Redis.from_url(redis_url, socket_timeout=3.0)
            client.ping()

            from langgraph.checkpoint.redis import RedisSaver

            saver = RedisSaver(redis_url=redis_url)
            print("[Checkpointer] Redis 连接成功。成功加载 RedisSaver 持久化记忆。")
            return saver
        except Exception as e:
            print(
                f"[Checkpointer] Redis 连接或初始化失败 (URL: {redis_url}): {e}。将降级使用 MemorySaver。"
            )
    else:
        print("[Checkpointer] 未配置 REDIS_URL，当前使用的是 MemorySaver 记忆方案。")
    return MemorySaver()


# 编译并导出状态图应用
app_graph = workflow.compile(checkpointer=get_checkpointer())
