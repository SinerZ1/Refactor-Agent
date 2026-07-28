import asyncio
from collections.abc import Callable, Coroutine, Iterator
from typing import Any, cast

from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.types import Command

from .budgets import empty_run_usage
from .events import AgentEvent, make_agent_event
from .plans import RefactorPlan, find_plan_task_id
from .state import State, get_message_text
from .workflow import app_graph

# 显式导出
__all__ = ["app_graph", "simple_refactor", "stream_refactor"]


def simple_refactor(code: str, config: RunnableConfig | None = None) -> str:
    """
    同步阻塞重构 (兼容旧接口)
    """
    run_config: RunnableConfig = config or {
        "configurable": {"thread_id": "default_sync_session"}
    }
    input_msg = HumanMessage(content=f"请帮我处理以下代码或路径：\n\n{code}")
    initial_state: State = {
        "messages": [input_msg],
        "retry_count": 0,
        "review_protocol_errors": 0,
        "review_status": "running",
        "change_records": [],
        "test_run_records": [],
        "review_evidence": None,
        "refactor_plan": None,
        "plan_error": None,
        "run_usage": empty_run_usage(),
        "budget_exceeded": False,
        "budget_reason": None,
    }
    try:
        final_state = app_graph.invoke(initial_state, run_config)
        return get_message_text(final_state["messages"][-1].content)
    except Exception as e:
        return f"# [运行失败]\n# 错误信息: {e!s}"


def stream_refactor(
    code: str,
    thread_id: str = "default_session",
    config: RunnableConfig | None = None,
    ws_callback: Callable[[str, str, str], Coroutine[Any, Any, Any]] | None = None,
    main_loop: asyncio.AbstractEventLoop | None = None,
) -> Iterator[AgentEvent]:
    """
    使用 LangGraph 状态图执行多轮对话，并输出版本化结构事件。

    LangGraph 的 ``updates`` 是内部图状态增量；这里将其翻译为稳定的业务事件，
    避免浏览器理解 ToolMessage、节点名或提示词文本。它是 Graph State 与 UI
    之间的反腐层（Anti-Corruption Layer），后续替换图实现也不会污染前端。
    """
    # 构造配置，如果传入了更丰富的运行时配置，在这里进行 merge
    run_config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
    if config:
        if "recursion_limit" in config:
            run_config["recursion_limit"] = config["recursion_limit"]
        if "configurable" in config:
            run_config["configurable"].update(config["configurable"])

    yield make_agent_event(
        "run.started",
        "重构工作流已启动",
        node="workflow",
    )
    current_state = app_graph.get_state(run_config)
    active_plan: RefactorPlan | None = current_state.values.get("refactor_plan")

    # 阶段 2：检测是否处于挂起（Interrupt）状态，并根据是否有 resume_value 执行恢复运行
    stream_input: State | Command[Any]
    if current_state.interrupts:
        resume_value = run_config["configurable"].get("resume_value")
        if resume_value is not None:
            stream_input = Command(resume=resume_value)
        else:
            # 如果处于挂起状态但未传 approval 状态，则终止流，防止重复触发
            yield make_agent_event(
                "log",
                "状态机已挂起，正在等待用户的人机协作审批放行信号",
                node="workflow",
            )
            return
    else:
        if not current_state.values or not current_state.values.get("messages"):
            stream_input = State(
                messages=[
                    HumanMessage(content=f"请帮我处理以下代码或路径：\n\n{code}")
                ],
                retry_count=0,
                review_protocol_errors=0,
                review_status="running",
                change_records=[],
                test_run_records=[],
                review_evidence=None,
                refactor_plan=None,
                plan_error=None,
                run_usage=empty_run_usage(),
                budget_exceeded=False,
                budget_reason=None,
            )
        else:
            stream_input = State(
                messages=[HumanMessage(content=code)],
                retry_count=0,
                review_protocol_errors=0,
                review_status="running",
                change_records=[],
                test_run_records=[],
                review_evidence=None,
                refactor_plan=None,
                plan_error=None,
                run_usage=empty_run_usage(),
                budget_exceeded=False,
                budget_reason=None,
            )

    try:
        # 传入初始消息字典、多轮追问消息或恢复 Command 进行流式迭代
        for chunk in app_graph.stream(
            stream_input,
            run_config,
            stream_mode="updates",
        ):
            for node_name, node_output in chunk.items():
                if node_name in [
                    "architect_tools",
                    "developer_tools",
                    "reviewer_tools",
                ]:
                    owner_node = node_name.removesuffix("_tools")
                    for msg in node_output.get("messages", []):
                        if not isinstance(msg, ToolMessage):
                            continue
                        artifact = (
                            msg.artifact if isinstance(msg.artifact, dict) else {}
                        )
                        success = (
                            msg.status != "error"
                            and artifact.get("success", True) is not False
                        )
                        tool_name = str(msg.name or "unknown_tool")
                        result_text = get_message_text(msg.content)
                        payload = dict(artifact)
                        event_task_id = find_plan_task_id(
                            active_plan, artifact.get("file_path")
                        )
                        if artifact.get("failure_kind") == "approval_rejected":
                            yield make_agent_event(
                                "approval.rejected",
                                "用户拒绝了文件写入审批",
                                level="error",
                                node=owner_node,
                                task_id=event_task_id,
                                tool=tool_name,
                                success=False,
                                payload=payload,
                            )
                        yield make_agent_event(
                            "tool.completed" if success else "tool.failed",
                            f"工具 `{tool_name}` 运行结果:\n{result_text}",
                            level="success" if success else "error",
                            node=owner_node,
                            task_id=event_task_id,
                            tool=tool_name,
                            success=success,
                            payload=payload,
                        )
                elif node_name in ["architect", "developer", "reviewer"]:
                    task_id = (
                        "architect_task"
                        if node_name == "architect"
                        else "reviewer_task" if node_name == "reviewer" else None
                    )
                    yield make_agent_event(
                        "task.started",
                        f"{node_name.capitalize()} 开始处理任务",
                        node=node_name,
                        task_id=task_id,
                    )
                    usage = node_output.get("run_usage")
                    limits = node_output.get("run_budget_limits")
                    if isinstance(usage, dict) and isinstance(limits, dict):
                        unmetered_note = (
                            f"，其中 {usage.get('unmetered_steps', 0)} 步未返回 Token 计量"
                            if usage.get("unmetered_steps")
                            else ""
                        )
                        yield make_agent_event(
                            "run.usage.updated",
                            (
                                f"运行用量：Agent {usage.get('agent_steps', 0)}/"
                                f"{limits.get('max_agent_steps', 0)}，工具 "
                                f"{usage.get('tool_calls', 0)}/"
                                f"{limits.get('max_tool_calls', 0)}，Token "
                                f"{usage.get('total_tokens', 0)}/"
                                f"{limits.get('max_total_tokens', 0)}{unmetered_note}"
                            ),
                            node=node_name,
                            task_id=task_id,
                            payload={"usage": usage, "limits": limits},
                        )
                    if node_output.get("budget_exceeded"):
                        yield make_agent_event(
                            "run.budget.exceeded",
                            str(node_output.get("budget_reason") or "运行预算已耗尽"),
                            level="error",
                            node=node_name,
                            task_id=task_id,
                            success=False,
                            payload={
                                "usage": usage or {},
                                "limits": limits or {},
                                "reason": node_output.get("budget_reason"),
                            },
                        )
                    if node_name == "architect" and (
                        "refactor_plan" in node_output or "plan_error" in node_output
                    ):
                        candidate_plan = node_output.get("refactor_plan")
                        active_plan = (
                            cast(RefactorPlan, candidate_plan)
                            if isinstance(candidate_plan, dict)
                            else None
                        )
                        if active_plan is not None:
                            yield make_agent_event(
                                "plan.created",
                                f"Architect 已生成 {len(active_plan['tasks'])} 个重构任务",
                                level="success",
                                node=node_name,
                                task_id="architect_task",
                                success=True,
                                payload={"plan": active_plan},
                            )
                        elif node_output.get("plan_error"):
                            yield make_agent_event(
                                "log",
                                str(node_output["plan_error"]),
                                level="error",
                                node=node_name,
                                task_id="architect_task",
                                success=False,
                            )
                    prefix = f"\n=== 【{node_name.upper()} 正在发言】 ===\n"
                    for msg in node_output.get("messages", []):
                        if msg.tool_calls:
                            for tc in msg.tool_calls:
                                # 写入内容属于审批信荷，不应再复制进普通事件流。这里只暴露
                                # 调度所需参数与体积，兼顾可观测性、传输成本和日志最小披露。
                                public_args = dict(tc["args"])
                                content = public_args.pop("content", None)
                                if isinstance(content, str):
                                    public_args["content_chars"] = len(content)
                                yield make_agent_event(
                                    "tool.started",
                                    f"{node_name.capitalize()} 决定调用工具 `{tc['name']}`",
                                    node=node_name,
                                    task_id=(
                                        find_plan_task_id(
                                            active_plan, tc["args"].get("file_path")
                                        )
                                        or task_id
                                    ),
                                    tool=tc["name"],
                                    payload={"args": public_args},
                                )
                        else:
                            # 阶段 3: A2A 多角色聊天室，通过 ws_callback 广播最终发言到 WebSocket 中
                            sender_name = (
                                "CoderAgent"
                                if node_name == "developer"
                                else (
                                    "ReviewerAgent"
                                    if node_name == "reviewer"
                                    else "ArchitectAgent"
                                )
                            )
                            msg_text = get_message_text(msg.content)
                            if ws_callback:
                                try:
                                    loop = main_loop
                                    if loop and loop.is_running():
                                        asyncio.run_coroutine_threadsafe(
                                            ws_callback(
                                                thread_id, sender_name, msg_text
                                            ),
                                            loop,
                                        )
                                except Exception as ex:
                                    print(
                                        f"[stream_refactor] Failed to trigger A2A chat ws_callback: {ex}"
                                    )

                            # 最终回答，使用打字机流式效果输出
                            text_content = prefix + msg_text + "\n"
                            chunk_size = 12
                            for i in range(0, len(text_content), chunk_size):
                                yield make_agent_event(
                                    "agent.message.delta",
                                    text_content[i : i + chunk_size],
                                    node=node_name,
                                )
                            if node_name == "reviewer":
                                if "【REFACTOR_FAIL】" in msg_text:
                                    yield make_agent_event(
                                        "review.failed",
                                        "Reviewer 审查未通过",
                                        level="error",
                                        node=node_name,
                                        task_id=task_id,
                                        success=False,
                                    )
                            elif node_name == "architect":
                                yield make_agent_event(
                                    "task.completed",
                                    "Architect 已完成重构分析",
                                    level="success",
                                    node=node_name,
                                    task_id=task_id,
                                    success=True,
                                )
                elif node_name == "developer_retry":
                    yield make_agent_event(
                        "run.retrying",
                        "检测到审查未通过，已启动开发者重试节点",
                        node="developer",
                        task_id="reviewer_task",
                    )
                elif node_name == "reviewer_protocol_retry":
                    yield make_agent_event(
                        "run.retrying",
                        "Reviewer 结论缺少有效协议或测试证据，正在请求其修正",
                        node="reviewer",
                        task_id="reviewer_task",
                    )
                elif node_name == "finalize_review_success":
                    if node_output.get("review_status") == "success":
                        yield make_agent_event(
                            "review.passed",
                            "Reviewer 结构化证据门禁验证通过",
                            level="success",
                            node="reviewer",
                            task_id="reviewer_task",
                            success=True,
                            payload={
                                "evidence": node_output.get("review_evidence") or {}
                            },
                        )
                    else:
                        for msg in node_output.get("messages", []):
                            yield make_agent_event(
                                "review.failed",
                                get_message_text(msg.content),
                                level="error",
                                node="reviewer",
                                task_id="reviewer_task",
                                success=False,
                            )
                elif node_name == "finalize_review_failure":
                    for msg in node_output.get("messages", []):
                        yield make_agent_event(
                            "agent.message.delta",
                            f"\n{get_message_text(msg.content)}\n",
                            node="reviewer",
                        )
                elif node_name == "finalize_budget_failure":
                    for msg in node_output.get("messages", []):
                        yield make_agent_event(
                            "agent.message.delta",
                            f"\n{get_message_text(msg.content)}\n",
                            node="workflow",
                        )
                elif node_name == "__interrupt__":
                    yield make_agent_event(
                        "log",
                        "触发人机协作审查 (HITL)，请在弹窗中确认文件写入操作",
                        node="developer",
                    )
    except Exception as e:
        yield make_agent_event(
            "run.failed",
            f"重构工作流运行失败: {e!s}",
            level="error",
            node="workflow",
            success=False,
        )
