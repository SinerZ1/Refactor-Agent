from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from pydantic_settings import BaseSettings
import os
from dotenv import load_dotenv

# 加载 .env 环境变量
load_dotenv()

class Settings(BaseSettings):
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    openai_base_url: str = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    model_name: str = os.getenv("MODEL_NAME", "gpt-4o-mini")

settings = Settings()

# 初始化 LLM 客户端
llm = ChatOpenAI(
    api_key=settings.openai_api_key,
    base_url=settings.openai_base_url,
    model=settings.model_name,
    temperature=0.2, # 重构代码需要相对确定的输出
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
    if not settings.openai_api_key or settings.openai_api_key == "your_api_key_here":
        return "# [错误] 请在 backend/.env 文件中配置你的 API 密钥 (OPENAI_API_KEY)。"
        
    try:
        response = refactor_chain.invoke({"code": code})
        # 返回生成的内容
        if hasattr(response, 'content'):
            return response.content
        return str(response)
    except Exception as e:
        return f"# [调用模型失败]\n# 错误信息: {str(e)}\n# 请检查你的网络或 API Key / Base URL 配置。"
