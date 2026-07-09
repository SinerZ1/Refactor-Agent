from langgraph.graph import END

from .state import State

# ============================================================
# 教学说明: 条件路由边 (Conditional Edges) 与状态决策流转
# ------------------------------------------------------------
# 相当于 Hello-Agents 课程中的运行时动态决策机制。
# 根据前一个节点输出的消息内容或是否存在工具调用（Tool Calls），
# 返回下一跳的节点名称。Reviewer 路由通过检测 `【REFACTOR_FAIL】`
# 自主决定是退回重试还是走向 `END` 终点，形成完全自治的重试闭环。
# ============================================================


def route_architect(state: State):
    """
    根据 Architect 的最后一条消息决定是调用其绑定的工具，还是流转到 Developer
    """
    last_message = state["messages"][-1]
    if last_message.tool_calls:
        return "architect_tools"
    return "developer"


def route_developer(state: State):
    """
    根据 Developer 的最后一条消息决定是调用写文件等工具，还是流转到 Reviewer 审查
    """
    last_message = state["messages"][-1]
    if last_message.tool_calls:
        return "developer_tools"
    return "reviewer"


def route_reviewer(state: State):
    """
    根据 Reviewer 单元测试运行和代码审查结果，判断：
    - 如果工具待执行，调用 reviewer_tools (如 run_unit_tests)；
    - 如果包含 【REFACTOR_FAIL】 且重试未超 3 次，回退至开发重试节点；
    - 否则优雅走向结束 (END)。
    """
    last_message = state["messages"][-1]
    if last_message.tool_calls:
        return "reviewer_tools"

    content = last_message.content or ""
    if "【REFACTOR_FAIL】" in content:
        retries = state.get("retry_count", 0)
        if retries < 3:
            return "developer_retry"
    return END
