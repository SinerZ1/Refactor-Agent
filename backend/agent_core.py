from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from pydantic_settings import BaseSettings
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.tools import tool
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from typing import Annotated, TypedDict, Literal
import os
import subprocess
import json
from dotenv import load_dotenv

# 引入 LangGraph 核心组件
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from langgraph.checkpoint.memory import MemorySaver

# 加载 .env 环境变量
load_dotenv()

class Settings(BaseSettings):
    use_vertex: bool = os.getenv("GOOGLE_VERTEX_AI", "false").lower() == "true"
    vertex_model_name: str = os.getenv("GOOGLE_VERTEX_MODEL_NAME", "gemini-1.5-flash")
    vertex_project: str = os.getenv("GOOGLE_VERTEX_PROJECT_ID", "")
    
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    openai_base_url: str = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    model_name: str = os.getenv("MODEL_NAME", "gpt-4o-mini")

settings = Settings()

# 初始化 LLM 客户端
if settings.use_vertex:
    llm = ChatGoogleGenerativeAI(
        model=settings.vertex_model_name,
        project=settings.vertex_project,
        temperature=0.2,
    )
else:
    llm = ChatOpenAI(
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url,
        model=settings.model_name,
        temperature=0.2,
    )

# ============================================================
# 工具定义 (Tools)
# ============================================================

@tool
def read_code_file(file_path: str) -> str:
    """
    读取指定路径下的本地代码文件内容。当用户指定文件路径，或者你想查看某个具体文件的代码时使用。
    """
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        return f"读取文件失败: {str(e)}"

@tool
def write_code_file(file_path: str, content: str) -> str:
    """
    将重构后的完整代码写入到指定的本地文件路径中。当重构完成并且你需要保存修改时使用。
    """
    try:
        dir_name = os.path.dirname(os.path.abspath(file_path))
        if dir_name:
            os.makedirs(dir_name, exist_ok=True)
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(content)
        return f"成功将重构代码写入到文件: {file_path}"
    except Exception as e:
        return f"写入文件失败: {str(e)}"

@tool
def run_unit_tests(test_command: str = "pytest") -> str:
    """
    执行本项目的测试命令（例如 pytest）来运行单元测试，验证重构后的代码是否符合质量标准。
    """
    try:
        result = subprocess.run(
            test_command,
            shell=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=20
        )
        output = (result.stdout or "") + "\n" + (result.stderr or "")
        return f"测试执行完成。退出代码 (Exit Code): {result.returncode}\n输出内容:\n{output}"
    except Exception as e:
        return f"运行测试失败: {str(e)}"

tools = [read_code_file, write_code_file, run_unit_tests]
llm_with_tools = llm.bind_tools(tools)

# ============================================================
# 阶段 4: LangGraph 状态图与持久化 (Memory/Redis)
# ============================================================

# 系统级指令
SYSTEM_PROMPT = """你是一个能够使用本地工具并拥有对话记忆的资深 Python 架构师。
你可以读取文件、修改文件、运行测试。如果你要分析或重构某个路径下的代码，请先用 `read_code_file` 读取它。
在对代码进行优化、修改、或者根据用户的后续意见进行局部的微调后，你要写回文件并运行 `run_unit_tests` 进行测试。

【注意】：
- 你们正在进行一个多轮对话。如果用户说“再把 add 方法重命名为 sum”，说明是在针对刚才的代码进行后续修改。
- 请直接完成修改，写回原文件，并通过测试后告诉用户。
- 最终回复时，请直接给出最新的完整代码。
"""

# 定义状态 (State) 字典，messages 字段会自动追加新消息
class State(TypedDict):
    messages: Annotated[list, add_messages]

# 1. 定义 Agent 节点逻辑
def call_agent(state: State):
    messages = state["messages"]
    # 确保首条消息前有 System 指令
    if not any(isinstance(m, SystemMessage) for m in messages):
        messages = [SystemMessage(content=SYSTEM_PROMPT)] + messages
    
    response = llm_with_tools.invoke(messages)
    return {"messages": [response]}

# 2. 定义条件路由判断函数
def should_continue(state: State) -> Literal["tools", "__end__"]:
    last_message = state["messages"][-1]
    if last_message.tool_calls:
        return "tools"
    return "__end__"

# 3. 构建 LangGraph 状态图
workflow = StateGraph(State)

# 添加节点
workflow.add_node("agent", call_agent)
workflow.add_node("tools", ToolNode(tools))

# 设置连线
workflow.add_edge(START, "agent")
workflow.add_conditional_edges("agent", should_continue)
workflow.add_edge("tools", "agent")

# 4. 初始化持久化 Checkpointer (双保险支持)
def get_checkpointer():
    redis_url = os.getenv("REDIS_URL")
    if redis_url:
        try:
            from langgraph.checkpoint.redis import RedisSaver
            saver = RedisSaver.from_conn_string(redis_url)
            print("[Checkpointer] 成功加载 RedisSaver 持久化记忆。")
            return saver
        except Exception as e:
            print(f"[Checkpointer] 初始化 RedisSaver 失败: {e}。将降级使用 MemorySaver。")
    return MemorySaver()

# 编译 Graph 状态机
app_graph = workflow.compile(checkpointer=get_checkpointer())

# ============================================================
# 外层调用适配方法
# ============================================================

def simple_refactor(code: str) -> str:
    """
    同步阻塞重构 (阶段 1-3 遗留兼容)
    """
    config = {"configurable": {"thread_id": "default_sync_session"}}
    input_msg = HumanMessage(content=f"请帮我处理以下代码或路径：\n\n{code}")
    try:
        final_state = app_graph.invoke({"messages": [input_msg]}, config)
        return final_state["messages"][-1].content
    except Exception as e:
        return f"# [运行失败]\n# 错误信息: {str(e)}"

def stream_refactor(code: str, thread_id: str = "default_session"):
    """
    使用 LangGraph 状态图执行多轮对话流式生成器
    """
    config = {"configurable": {"thread_id": thread_id}}
    
    # 检查当前会话是否存在历史。如果没有，则是首轮代码重构；如果有，则是后续对话指令。
    current_state = app_graph.get_state(config)
    if not current_state.values or not current_state.values.get("messages"):
        input_msg = HumanMessage(content=f"请帮我处理以下代码或路径：\n\n{code}")
    else:
        input_msg = HumanMessage(content=code)

    try:
        # 使用 astream 或 stream 迭代状态变更
        for chunk in app_graph.stream({"messages": [input_msg]}, config, stream_mode="updates"):
            for node_name, node_output in chunk.items():
                if node_name == "tools":
                    # 工具执行节点完毕，向前端推送运行日志
                    for msg in node_output.get("messages", []):
                        yield f"[SUCCESS] 工具 `{msg.name}` 运行结果:\n{msg.content}\n"
                elif node_name == "agent":
                    # Agent 运行，检测是否触发工具调用
                    for msg in node_output.get("messages", []):
                        if msg.tool_calls:
                            for tc in msg.tool_calls:
                                yield f"[INFO] Agent 决定调用工具 `{tc['name']}`，参数为: {json.dumps(tc['args'], ensure_ascii=False)}\n"
                        else:
                            # 最终回答，使用打字机流式效果输出
                            text_content = msg.content
                            chunk_size = 8
                            for i in range(0, len(text_content), chunk_size):
                                yield text_content[i:i+chunk_size]
    except Exception as e:
        yield f"\n# [运行失败]\n# 错误信息: {str(e)}\n"
