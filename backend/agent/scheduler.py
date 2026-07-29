import json
from typing import Any

from langchain_core.messages import HumanMessage

from .plans import RefactorPlan
from .state import State, TaskFailure, TaskTransition

MAX_TASK_RETRIES = 3


def initialize_plan_execution(
    plan: RefactorPlan | None,
    plan_error: str | None,
) -> dict[str, Any]:
    """把 Architect 的计划转换为可持久化的执行控制面。

    计划缺失或校验失败时显式进入 ``fallback``，保留旧版单 Developer 执行链。
    这是兼容路径而非伪造一个动态任务，因为前端和审计日志必须能区分“真实 DAG”
    与“模型没有给出可执行计划”。
    """

    if plan is None:
        return {
            "task_statuses": {},
            "active_task_id": None,
            "completed_task_ids": [],
            "task_failures": {},
            "task_retry_counts": {},
            "plan_status": "fallback",
            "active_task_write_succeeded": False,
            "active_task_failure_reason": plan_error,
            "task_transition": None,
        }
    return {
        "task_statuses": {task["id"]: "pending" for task in plan["tasks"]},
        "active_task_id": None,
        "completed_task_ids": [],
        "task_failures": {},
        "task_retry_counts": {task["id"]: 0 for task in plan["tasks"]},
        "plan_status": "pending",
        "active_task_write_succeeded": False,
        "active_task_failure_reason": None,
        "task_transition": None,
    }


def _downstream_task_ids(plan: RefactorPlan, roots: set[str]) -> list[str]:
    """按计划原始稳定顺序返回根任务及其传递下游。"""

    affected = set(roots)
    changed = True
    while changed:
        changed = False
        for task in plan["tasks"]:
            if task["id"] in affected:
                continue
            if any(dependency in affected for dependency in task["dependencies"]):
                affected.add(task["id"])
                changed = True
    return [task["id"] for task in plan["tasks"] if task["id"] in affected]


def schedule_next_task_node(state: State) -> dict[str, Any]:
    """选择第一个 ready task，实现稳定、确定性的串行拓扑调度。"""

    plan = state.get("refactor_plan")
    if plan is None or state.get("plan_status") == "fallback":
        return {"task_transition": None}
    if state.get("plan_status") in {"completed", "failed"}:
        return {"active_task_id": None, "task_transition": None}

    statuses = dict(state.get("task_statuses", {}))
    completed = set(state.get("completed_task_ids", []))
    if len(completed) == len(plan["tasks"]):
        return {
            "active_task_id": None,
            "plan_status": "completed",
            "task_transition": None,
        }

    for task in plan["tasks"]:
        task_id = task["id"]
        task_status = statuses.get(task_id)
        retry_count = state.get("task_retry_counts", {}).get(task_id, 0)
        if task_status not in {"pending", "failed"} or retry_count >= MAX_TASK_RETRIES:
            continue
        if all(dependency in completed for dependency in task["dependencies"]):
            statuses[task_id] = "running"
            transition: TaskTransition = {
                "task_id": task_id,
                "status": "running",
                "retry_count": retry_count,
            }
            return {
                "messages": [
                    HumanMessage(
                        content=(
                            "[CURRENT_TASK_CONTEXT]\n"
                            f"任务 ID: {task_id}\n"
                            f"标题: {task['title']}\n"
                            f"目标: {task['description']}\n"
                            f"唯一允许写入路径: {task['file_path']}\n"
                            f"已完成依赖: {', '.join(task['dependencies']) or '无'}\n"
                            "只完成当前任务；成功写入指定文件后给出简短总结。"
                        )
                    )
                ],
                "task_statuses": statuses,
                "active_task_id": task_id,
                "plan_status": "running",
                "active_task_write_succeeded": False,
                "active_task_failure_reason": None,
                "task_transition": transition,
            }

    # 合法 DAG 只有在上游已经失败/阻断时才会走到这里。防御性地把剩余任务
    # 固化为 blocked，避免状态机无 ready task 却无限空转。
    blocked_ids = [
        task["id"] for task in plan["tasks"] if statuses.get(task["id"]) == "pending"
    ]
    for task_id in blocked_ids:
        statuses[task_id] = "blocked"
    return {
        "task_statuses": statuses,
        "active_task_id": None,
        "plan_status": "failed",
        "task_transition": (
            {
                "task_id": blocked_ids[0],
                "status": "blocked",
                "reason": "没有可执行任务，依赖链已被失败任务阻断",
                "blocked_task_ids": blocked_ids,
            }
            if blocked_ids
            else None
        ),
    }


def complete_active_task_node(state: State) -> dict[str, Any]:
    """以写工具事实闭环一次任务尝试，并在重试耗尽后阻断传递下游。"""

    plan = state.get("refactor_plan")
    task_id = state.get("active_task_id")
    if plan is None or not task_id:
        return {"task_transition": None}

    statuses = dict(state.get("task_statuses", {}))
    retries = dict(state.get("task_retry_counts", {}))
    failures = dict(state.get("task_failures", {}))
    completed = list(state.get("completed_task_ids", []))

    # 预算熔断优先于写入事实：成功写入后仍需要一个模型回合闭合 ToolCall。
    # 若该回合无法执行，任务对外没有可验证的完成声明，因此必须按失败终止。
    if state.get("active_task_write_succeeded") and not state.get("budget_exceeded"):
        statuses[task_id] = "completed"
        if task_id not in completed:
            completed.append(task_id)
        failures.pop(task_id, None)
        all_completed = len(completed) == len(plan["tasks"])
        transition: TaskTransition = {
            "task_id": task_id,
            "status": "completed",
            "retry_count": retries.get(task_id, 0),
        }
        return {
            "task_statuses": statuses,
            "active_task_id": None,
            "completed_task_ids": completed,
            "task_failures": failures,
            "plan_status": "completed" if all_completed else "running",
            "active_task_write_succeeded": False,
            "active_task_failure_reason": None,
            "task_transition": transition,
        }

    retry_count = retries.get(task_id, 0) + 1
    retries[task_id] = retry_count
    reason = (
        state.get("budget_reason")
        if state.get("budget_exceeded")
        else state.get("active_task_failure_reason")
    ) or "Developer 未成功写入当前任务指定文件"
    failure_kind = (
        "budget_exceeded" if state.get("budget_exceeded") else "task_incomplete"
    )
    failure: TaskFailure = {
        "reason": reason,
        "failure_kind": failure_kind,
        "retry_count": retry_count,
    }
    failures[task_id] = failure

    exhausted = state.get("budget_exceeded", False) or retry_count >= MAX_TASK_RETRIES
    if not exhausted:
        statuses[task_id] = "failed"
        transition = {
            "task_id": task_id,
            "status": "failed",
            "reason": reason,
            "retry_count": retry_count,
        }
        return {
            "task_statuses": statuses,
            "active_task_id": None,
            "task_failures": failures,
            "task_retry_counts": retries,
            "plan_status": "running",
            "active_task_write_succeeded": False,
            "active_task_failure_reason": None,
            "task_transition": transition,
        }

    statuses[task_id] = "failed"
    affected = _downstream_task_ids(plan, {task_id})
    blocked_ids = [candidate for candidate in affected if candidate != task_id]
    for blocked_id in blocked_ids:
        if statuses.get(blocked_id) != "failed":
            statuses[blocked_id] = "blocked"
    transition = {
        "task_id": task_id,
        "status": "failed",
        "reason": reason,
        "retry_count": retry_count,
        "blocked_task_ids": blocked_ids,
    }
    return {
        "task_statuses": statuses,
        "active_task_id": None,
        "task_failures": failures,
        "task_retry_counts": retries,
        "plan_status": "failed",
        "active_task_write_succeeded": False,
        "active_task_failure_reason": None,
        "task_transition": transition,
    }


def parse_reviewer_failure_result(
    content: str,
) -> tuple[list[str] | None, str | None, str | None]:
    """从 Reviewer 文本中提取唯一结构化失败结果。

    允许协议标记前后有解释文字，但只接受一个满足 schema 的 JSON 对象。任何歧义
    都返回错误，由调用方保守地重开计划任务，而不是把不可解析回复当成成功。
    """

    decoder = json.JSONDecoder()
    candidates: list[dict[str, Any]] = []
    for index, character in enumerate(content):
        if character != "{":
            continue
        try:
            value, _ = decoder.raw_decode(content[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            candidates.append(value)
    if len(candidates) != 1:
        return None, None, "Reviewer 必须返回一个且仅一个结构化失败 JSON 对象"

    value = candidates[0]
    failed_ids = value.get("failed_task_ids")
    summary = value.get("summary")
    if (
        value.get("status") != "failed"
        or not isinstance(failed_ids, list)
        or not failed_ids
        or any(not isinstance(task_id, str) or not task_id for task_id in failed_ids)
        or len(set(failed_ids)) != len(failed_ids)
        or not isinstance(summary, str)
        or not summary.strip()
    ):
        return None, None, "Reviewer 结构化失败结果不符合协议"
    return failed_ids, summary.strip(), None


def reopen_tasks_after_review(state: State) -> dict[str, Any]:
    """校验 Reviewer 指定任务，并把它们及传递下游恢复为 pending。"""

    plan = state.get("refactor_plan")
    if plan is None or state.get("plan_status") == "fallback":
        return {}

    from .state import get_message_text

    content = get_message_text(state["messages"][-1].content)
    failed_ids, summary, protocol_error = parse_reviewer_failure_result(content)
    known_ids = {task["id"] for task in plan["tasks"]}
    if failed_ids is None or not set(failed_ids).issubset(known_ids):
        protocol_error = protocol_error or "Reviewer 返回了当前计划之外的任务 ID"
        failed_ids = [task["id"] for task in plan["tasks"]]
        summary = protocol_error

    reopened_ids = _downstream_task_ids(plan, set(failed_ids))
    statuses = dict(state.get("task_statuses", {}))
    failures = dict(state.get("task_failures", {}))
    for task_id in reopened_ids:
        statuses[task_id] = "pending"
        failures.pop(task_id, None)

    completed = [
        task_id
        for task_id in state.get("completed_task_ids", [])
        if task_id not in set(reopened_ids)
    ]
    transition: TaskTransition = {
        "task_id": failed_ids[0],
        "status": "failed",
        "reason": summary or "Reviewer 打回任务",
        "blocked_task_ids": [
            task_id for task_id in reopened_ids if task_id not in set(failed_ids)
        ],
    }
    return {
        "task_statuses": statuses,
        "active_task_id": None,
        "completed_task_ids": completed,
        "task_failures": failures,
        "plan_status": "running",
        "active_task_write_succeeded": False,
        "active_task_failure_reason": None,
        "task_transition": transition,
    }
