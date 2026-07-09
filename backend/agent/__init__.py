import asyncio
import json

from langchain_core.messages import HumanMessage

from .workflow import app_graph

# 显式导出
__all__ = ["app_graph", "simple_refactor", "stream_refactor"]


def simple_refactor(code: str, config: dict = None) -> str:
    """
    同步阻塞重构 (兼容旧接口)
    """
    run_config = config or {"configurable": {"thread_id": "default_sync_session"}}
    input_msg = HumanMessage(content=f"请帮我处理以下代码或路径：\n\n{code}")
    try:
        final_state = app_graph.invoke(
            {"messages": [input_msg], "retry_count": 0}, run_config
        )
        return final_state["messages"][-1].content
    except Exception as e:
        return f"# [运行失败]\n# 错误信息: {str(e)}"


def stream_refactor(code: str, thread_id: str = "default_session", config: dict = None):
    """
    使用 LangGraph 状态图执行多轮对话流式生成器
    """
    # 构造配置，如果传入了更丰富的运行时配置，在这里进行 merge
    run_config = {"configurable": {"thread_id": thread_id}}
    if config and "configurable" in config:
        run_config["configurable"].update(config["configurable"])

    current_state = app_graph.get_state(run_config)
    
    # 阶段 2：检测是否处于挂起（Interrupt）状态，并根据是否有 resume_value 执行恢复运行
    if current_state.interrupts:
        resume_value = run_config["configurable"].get("resume_value")
        if resume_value is not None:
            from langgraph.types import Command
            stream_input = Command(resume=resume_value)
        else:
            # 如果处于挂起状态但未传 approval 状态，则终止流，防止重复触发
            yield "[INFO] 状态机已挂起，正在等待用户的人机协作审批放行信号...\n"
            return
    else:
        if not current_state.values or not current_state.values.get("messages"):
            stream_input = {"messages": [HumanMessage(content=f"请帮我处理以下代码或路径：\n\n{code}")], "retry_count": 0}
        else:
            stream_input = {"messages": [HumanMessage(content=code)]}

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
                    # 工具执行节点完毕，向前端推送运行日志
                    for msg in node_output.get("messages", []):
                        yield f"[SUCCESS] 工具 `{msg.name}` 运行结果:\n{msg.content}\n"
                elif node_name in ["architect", "developer", "reviewer"]:
                    # Agent 运行，检测是否触发工具调用
                    prefix = f"\n=== 【{node_name.upper()} 正在发言】 ===\n"
                    for msg in node_output.get("messages", []):
                        if msg.tool_calls:
                            for tc in msg.tool_calls:
                                yield f"[INFO] {node_name.capitalize()} 决定调用工具 `{tc['name']}`，参数为: {json.dumps(tc['args'], ensure_ascii=False)}\n"
                        else:
                            # 阶段 3: A2A 多角色聊天室，通过 ws_callback 广播最终发言到 WebSocket 中
                            sender_name = "CoderAgent" if node_name == "developer" else ("ReviewerAgent" if node_name == "reviewer" else "ArchitectAgent")
                            ws_callback = run_config["configurable"].get("ws_callback")
                            if ws_callback:
                                try:
                                    loop = asyncio.get_event_loop()
                                    if loop.is_running():
                                        asyncio.run_coroutine_threadsafe(
                                            ws_callback(thread_id, sender_name, msg.content),
                                            loop
                                        )
                                except Exception as ex:
                                    print(f"[stream_refactor] Failed to trigger A2A chat ws_callback: {ex}")

                            # 最终回答，使用打字机流式效果输出
                            text_content = prefix + msg.content + "\n"
                            chunk_size = 12
                            for i in range(0, len(text_content), chunk_size):
                                yield text_content[i : i + chunk_size]
                elif node_name == "developer_retry":
                    yield "\n[SYSTEM] 检测到审查未通过，已启动开发者重试节点...\n"
    except Exception as e:
        yield f"\n# [运行失败]\n# 错误信息: {str(e)}\n"
