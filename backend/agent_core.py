from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from pydantic_settings import BaseSettings
from langchain_google_genai import ChatGoogleGenerativeAI

import os
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
        # langchain_google_genai ChatGoogleGenerativeAI may not accept `project` argument directly. 
        # If ADC is working, the SDK automatically picks up the project from the environment.
        temperature=0.2,
    )
else:
    llm = ChatOpenAI(
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url,
        model=settings.model_name,
        temperature=0.2,
    )


# 定义重构 Prompt
refactor_prompt = ChatPromptTemplate.from_messages([
    ("system", "你是一个资深的 Python 架构师和代码重构专家。你的任务是接收用户提供的 Python 代码，并将其重构为更符合 PEP 8 规范、更高效、更易读的代码。\n"
               "请直接返回重构后的代码，不需要过多的寒暄，可以用注释解释主要的修改点。"),
    ("user", "请帮我重构以下代码：\n\n{code}")
])

# 创建一个简单的 Chain (Runnable)
refactor_chain = refactor_prompt | llm

def simple_refactor(code: str) -> str:
    """
    接收原始代码，返回重构后的代码字符串
    """
    # 如果没有配置 API KEY，返回友好提示
    if not settings.use_vertex and (not settings.openai_api_key or settings.openai_api_key == "your_api_key_here"):
        return "# [错误] 请在 backend/.env 文件中配置你的 API 密钥。"
        
    try:
        response = refactor_chain.invoke({"code": code})
        
        # 修改提取逻辑：兼容 content 为 list 的情况
        if hasattr(response, 'content'):
            content = response.content
            if isinstance(content, str):
                return content
            elif isinstance(content, list):
                # 提取列表中所有 type='text' 的文本块并拼接
                return "".join(
                    block.get("text", "") 
                    for block in content 
                    if isinstance(block, dict) and block.get("type") == "text"
                )
            return str(content)
        
        return str(response)
    except Exception as e:
        return f"# [调用模型失败]\n# 错误信息: {str(e)}\n# 请检查你的网络或 API Key / Base URL 配置。"