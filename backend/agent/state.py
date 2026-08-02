import hashlib
import json
from typing import Annotated, Any, Literal, NotRequired, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages

from .budgets import RunBudgetLimits, RunUsage
from .plans import RefactorPlan

# ============================================================
# 教学说明: LangGraph Graph State 状态数据字典定义
# ------------------------------------------------------------
# 相当于 Hello-Agents 课程里讲过的 Context/Memory 存储协议，
# 在 LangGraph 中，我们通过 TypedDict 的 messages 属性以及 add_messages
# 实现追加/合并消息。当节点返回新的 messages 时，它们会被自动合并入全局状态。
# ============================================================


MAX_CHANGE_RECORDS = 12
MAX_TEST_RUN_RECORDS = 12


class ChangeRecord(TypedDict):
    """由写入工具生成、供 Reviewer 只读消费的可验证变更事实。"""

    file_path: str
    before_sha256: str
    after_sha256: str
    added_lines: int
    removed_lines: int
    unified_diff: str
    diff_truncated: bool


class TestRunRecord(TypedDict):
    """Reviewer 工具生成的测试事实；可信身份来自完整工作区快照。"""

    suite: str
    success: bool
    exit_code: int
    change_set_digest: str
    workspace_snapshot_digest: str
    workspace_stable: bool
    behavior_contract_included: bool
    success_marker_present: bool
    output_excerpt: str
    failure_kind: NotRequired[str]


class ReviewEvidence(TypedDict):
    """成功终态保留的最小证据清单，供 API、UI 与后续审计直接消费。"""

    changed_files: list[str]
    change_set_digest: str
    workspace_snapshot_digest: str
    test_suite: str
    test_success: bool
    test_exit_code: int


TaskExecutionStatus = Literal[
    "pending",
    "running",
    "completed",
    "failed",
    "blocked",
]
PlanExecutionStatus = Literal[
    "pending",
    "running",
    "completed",
    "failed",
    "fallback",
]


class TaskFailure(TypedDict):
    """任务调度器记录的确定性失败事实，不依赖模型自然语言供下游判断。"""

    reason: str
    failure_kind: str
    retry_count: int


class TaskTransition(TypedDict):
    """单个图节点产生的任务状态迁移，供事件反腐层精确投影到前端。"""

    task_id: str
    status: TaskExecutionStatus
    reason: NotRequired[str]
    retry_count: NotRequired[int]
    blocked_task_ids: NotRequired[list[str]]


def merge_change_records(
    existing: list[ChangeRecord], updates: list[ChangeRecord]
) -> list[ChangeRecord]:
    """追加变更事实并限制 Checkpoint 体积。

    这里没有覆盖旧记录，因为 Reviewer 需要看到多文件写入和失败重试的演进过程；
    同时只保留最近若干次写入，避免长会话把 Redis/MemorySaver 快照无限放大。
    """

    # 新一轮运行会显式传入空列表。把它解释为“清空本轮事实”，避免 Redis
    # Checkpoint 中上一轮写入被误当成当前任务证据；普通节点不返回该字段，因此不会误清空。
    if not updates:
        return []
    return (existing + updates)[-MAX_CHANGE_RECORDS:]


def merge_test_run_records(
    existing: list[TestRunRecord], updates: list[TestRunRecord]
) -> list[TestRunRecord]:
    """追加有界测试审计记录，并允许新一轮运行显式清空旧证据。"""

    if not updates:
        return []
    return (existing + updates)[-MAX_TEST_RUN_RECORDS:]


def compute_change_set_digest(change_records: list[ChangeRecord]) -> str:
    """计算当前有界写入审计序列的稳定摘要。

    摘要包含每次成功写入的路径与前后哈希，而不是只看最终文件内容。因此 Developer
    即使再次写入相同内容，写入序列也会变化。它继续服务 UI 与审计，但由于记录有界，
    不再承担代码内容身份；可信测试证据改由完整工作区快照摘要提供。
    """

    digest_payload = [
        {
            "file_path": record["file_path"],
            "before_sha256": record["before_sha256"],
            "after_sha256": record["after_sha256"],
        }
        for record in change_records
    ]
    canonical_json = json.dumps(
        digest_payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def review_evidence_errors(state: "State") -> list[str]:
    """以确定性规则检查 Reviewer 成功所需的结构化证据。"""

    change_records = state.get("change_records", [])
    if not change_records:
        return ["没有成功写入记录"]

    test_records = state.get("test_run_records", [])
    if not test_records:
        return ["尚未运行自动化测试"]

    latest_test = test_records[-1]
    current_digest = compute_change_set_digest(change_records)
    errors: list[str] = []
    if latest_test["change_set_digest"] != current_digest:
        errors.append("最新变更集合尚未测试，已有测试证据已失效")
    if latest_test["exit_code"] != 0 or not latest_test["success"]:
        errors.append(f"最新测试未通过（退出码 {latest_test['exit_code']}）")
    if not latest_test.get("workspace_stable", False):
        errors.append("测试过程改变了受管理源码，测试证据无效")
    if not latest_test.get("behavior_contract_included", False):
        errors.append("最新测试套件未包含可信 CodeSmells 行为契约")
    if not latest_test.get("success_marker_present", False):
        errors.append("最新测试记录缺少成功标记")

    workspace_id = state.get("workspace_id")
    if not workspace_id:
        errors.append("运行缺少隔离工作区，无法复核测试快照")
    else:
        try:
            # 延迟导入避免 state/workspace 的类型定义形成模块初始化环。
            from .workspace import build_workspace_snapshot

            current_snapshot = build_workspace_snapshot(workspace_id)
            if (
                latest_test.get("workspace_snapshot_digest")
                != current_snapshot["digest"]
            ):
                errors.append("当前工作区已偏离成功测试绑定的完整快照")
        except Exception as exc:
            errors.append(f"无法复核当前工作区快照: {exc}")
    return errors


class State(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]  # 合并/追加消息列表
    retry_count: int  # 记录 Reviewer 的重试次数
    review_protocol_errors: NotRequired[int]
    review_status: NotRequired[Literal["running", "success", "failed"]]
    change_records: NotRequired[Annotated[list[ChangeRecord], merge_change_records]]
    test_run_records: NotRequired[
        Annotated[list[TestRunRecord], merge_test_run_records]
    ]
    review_evidence: NotRequired[ReviewEvidence | None]
    # 计划是 Architect 输出的机器可读“控制面”；自然语言消息仍是 Developer 的“数据面”。
    # 二者并存使模型可解释性和状态机确定性不必互相牺牲。
    refactor_plan: NotRequired[RefactorPlan | None]
    plan_error: NotRequired[str | None]
    # 计划是执行控制面：状态字段随 Checkpointer 持久化，HITL 恢复时继续同一活动任务。
    task_statuses: NotRequired[dict[str, TaskExecutionStatus]]
    active_task_id: NotRequired[str | None]
    completed_task_ids: NotRequired[list[str]]
    task_failures: NotRequired[dict[str, TaskFailure]]
    task_retry_counts: NotRequired[dict[str, int]]
    plan_status: NotRequired[PlanExecutionStatus]
    active_task_write_succeeded: NotRequired[bool]
    active_task_failure_reason: NotRequired[str | None]
    task_transition: NotRequired[TaskTransition | None]
    run_usage: NotRequired[RunUsage]
    run_budget_limits: NotRequired[RunBudgetLimits]
    budget_exceeded: NotRequired[bool]
    budget_reason: NotRequired[str | None]
    # run 级隔离工作区是提交协议的一部分，而非进程内临时变量。把哈希、审批和
    # 补偿结果写入 Checkpoint，才能在 HITL 恢复后继续验证同一份不可变事实。
    workspace_id: NotRequired[str | None]
    baseline_file_hashes: NotRequired[dict[str, str]]
    final_file_hashes: NotRequired[dict[str, str]]
    final_workspace_snapshot_digest: NotRequired[str]
    aggregate_diff: NotRequired[str]
    workspace_changed_files: NotRequired[list[str]]
    workspace_approved: NotRequired[bool]
    workspace_applied: NotRequired[bool]
    workspace_rolled_back: NotRequired[bool]
    workspace_cleaned: NotRequired[bool]
    workspace_error: NotRequired[str | None]


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
