from .state import State

MAX_DEVELOPER_RETRIES = 3
MAX_REVIEW_PROTOCOL_ERRORS = 2

# ============================================================
# 教学说明: 条件路由边 (Conditional Edges) 与状态决策流转
# ------------------------------------------------------------
# 相当于 Hello-Agents 课程中的运行时动态决策机制。
# 根据前一个节点输出的消息内容或是否存在工具调用（Tool Calls），
# 返回下一跳的节点名称。Reviewer 路由把自然语言标记提升为状态机协议：
# 成功、失败与协议异常分别进入显式分支，防止无标记回答被误当作成功结束。
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
    - 如果包含 【REFACTOR_SUCCESS】，进入成功终态；
    - 如果缺少协议标记，有限次要求 Reviewer 修正，避免无限循环或静默成功。
    """
    last_message = state["messages"][-1]
    if last_message.tool_calls:
        return "reviewer_tools"

    content = last_message.content or ""
    if "【REFACTOR_FAIL】" in content:
        retries = state.get("retry_count", 0)
        if retries < MAX_DEVELOPER_RETRIES:
            return "developer_retry"
        return "finalize_review_failure"
    if "【REFACTOR_SUCCESS】" in content:
        return "finalize_review_success"

    protocol_errors = state.get("review_protocol_errors", 0)
    if protocol_errors < MAX_REVIEW_PROTOCOL_ERRORS:
        return "reviewer_protocol_retry"
    return "finalize_review_failure"
