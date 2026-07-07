from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from pydantic_settings import BaseSettings
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.tools import tool
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
import os
import subprocess
import json
from dotenv import load_dotenv

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

# 初始化 LLM 客户端，根据环境变量切换 Vertex AI 或 OpenAI
if settings.use_vertex:
    llm = ChatGoogleGenerativeAI(
        model=settings.vertex_model_name,
        project=settings.vertex_project, # 恢复 project 属性，用于 Google Vertex AI ADC 校验
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
# 阶段 3: 定义 Tool (工具)
# ============================================================

@tool
def read_code_file(file_path: str) -> str:
    """
    读取指定路径下的本地代码文件内容。当用户指定文件路径，或者你想查看某个具体文件的代码时使用。
    """
    try:
        # 支持相对路径和绝对路径
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
        # 自动创建父级目录
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
        # 执行测试命令并获取输出
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

# 整理工具列表
tools = [read_code_file, write_code_file, run_unit_tests]
tools_map = {tool.name: tool for tool in tools}

# 将工具绑定到 LLM 实例上，使模型具备进行 Tool Calling 的能力
llm_with_tools = llm.bind_tools(tools)

# ============================================================
# 核心重构与流式 Agent 逻辑
# ============================================================

# 系统指令：引导智能体如何结合工具进行重构
SYSTEM_PROMPT = """你是一个能够使用本地工具的资深 Python 架构师和代码重构专家。
你拥有以下工具：
1. `read_code_file`：读取本地代码文件。
2. `write_code_file`：将重构后的最新代码写回文件。
3. `run_unit_tests`：运行测试用例以验证代码。

【重构工作流指南】：
- 如果用户给出的代码是一个本地文件路径（如 backend/CodeSmells/Calculator.py），你应当**先使用** `read_code_file` 读取文件内容。
- 对代码进行深度分析后，编写优雅的重构版本。
- 重构完成后，你应当使用 `write_code_file` 将新代码写回原文件。
- 写回后，你应当使用 `run_unit_tests` 工具运行测试（例如运行 `pytest` 或是指定测试命令），验证修改是否破坏了原有功能。
- 在最后，向用户清晰地报告你的重构改动，并直接展示重构后的最终代码。使用 Markdown 代码块。

如果你只是接收到了一段纯代码（而不是文件路径），你无需写入文件或测试，直接在回复中输出重构后的代码即可。
"""

def simple_refactor(code: str) -> str:
    """
    接收原始代码/路径，进行一次完整的重构（同步阻塞版）
    """
    if not settings.use_vertex and (not settings.openai_api_key or settings.openai_api_key == "your_api_key_here"):
        return "# [错误] 请在 backend/.env 文件中配置你的 API 密钥。"

    messages = [
        SystemMessage(content=SYSTEM_PROMPT),
        HumanMessage(content=f"请帮我处理以下代码或路径：\n\n{code}")
    ]

    try:
        # 最大工具调用循环次数，防止无限循环
        for _ in range(5):
            response = llm_with_tools.invoke(messages)
            messages.append(response)

            # 如果没有工具调用请求，说明已经得到最终回复
            if not response.tool_calls:
                return response.content if isinstance(response.content, str) else str(response.content)

            # 处理工具调用
            for tool_call in response.tool_calls:
                tool_name = tool_call["name"]
                tool_args = tool_call["args"]
                tool_id = tool_call["id"]

                if tool_name in tools_map:
                    # 执行具体工具
                    tool_result = tools_map[tool_name].invoke(tool_args)
                    # 将工具返回结果作为 ToolMessage 存入历史，反馈给 LLM
                    messages.append(ToolMessage(content=str(tool_result), tool_call_id=tool_id))
                else:
                    messages.append(ToolMessage(content=f"错误：找不到工具 {tool_name}", tool_call_id=tool_id))
        
        return "Agent 超过了最大工具调用次数限制。"
    except Exception as e:
        return f"# [重构失败]\n# 错误信息: {str(e)}"

def stream_refactor(code: str):
    """
    流式重构代码并实时反馈工具调用日志 (SSE 友好生成器)
    """
    if not settings.use_vertex and (not settings.openai_api_key or settings.openai_api_key == "your_api_key_here"):
        yield "# [错误] 请在 backend/.env 文件中配置你的 API 密钥。"
        return

    messages = [
        SystemMessage(content=SYSTEM_PROMPT),
        HumanMessage(content=f"请帮我处理以下代码或路径：\n\n{code}")
    ]

    try:
        for _ in range(5):
            response = llm_with_tools.invoke(messages)
            messages.append(response)

            # 1. 检查 LLM 是否需要进行 Tool Calling
            if response.tool_calls:
                for tool_call in response.tool_calls:
                    tool_name = tool_call["name"]
                    tool_args = tool_call["args"]
                    tool_id = tool_call["id"]

                    # 将正在调用工具的“日志”推送给前端
                    yield f"[INFO] Agent 决定调用工具 `{tool_name}`，参数为: {json.dumps(tool_args, ensure_ascii=False)}\n"

                    if tool_name in tools_map:
                        tool_result = tools_map[tool_name].invoke(tool_args)
                        yield f"[SUCCESS] 工具 `{tool_name}` 运行结果:\n{tool_result}\n"
                        messages.append(ToolMessage(content=str(tool_result), tool_call_id=tool_id))
                    else:
                        err_msg = f"找不到工具 {tool_name}"
                        yield f"[ERROR] {err_msg}\n"
                        messages.append(ToolMessage(content=err_msg, tool_call_id=tool_id))
                
                # 继续下一次循环，让 LLM 根据工具结果做出下一步决定
                continue

            # 2. 如果没有 Tool Calling，说明进入了最终回答阶段，流式输出内容
            # 注意：因为之前的 invoke 是同步拿到的最终 AIMessage。如果它没有 tool_calls，
            # 我们可以通过 `stream` 接口对这轮对话启动流式，以获取打字机效果。
            try:
                for chunk in llm_with_tools.stream(messages[:-1]): # 传入不包含刚才 response 的 messages
                    if hasattr(chunk, 'content'):
                        content = chunk.content
                        if isinstance(content, str):
                            yield content
                        elif isinstance(content, list):
                            chunk_text = "".join(
                                block.get("text", "") 
                                for block in content 
                                if isinstance(block, dict) and block.get("type") == "text"
                            )
                            yield chunk_text
                    else:
                        yield str(chunk)
            except Exception as e:
                # 降级：如果流式 stream 异常，则直接返回 content
                yield response.content if isinstance(response.content, str) else str(response.content)
            
            # 完成最终回复，退出循环
            break
            
    except Exception as e:
        yield f"\n# [运行失败]\n# 错误信息: {str(e)}\n"
