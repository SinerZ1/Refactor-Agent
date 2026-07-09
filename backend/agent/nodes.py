from langchain_openai import ChatOpenAI
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import SystemMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
import os

# 使用相对导入保证子包高内聚、易移植
from .state import State
from .prompts import ARCHITECT_PROMPT, DEVELOPER_PROMPT, REVIEWER_PROMPT
from .tools import architect_tools, developer_tools, reviewer_tools

# ============================================================
# 教学说明: 智能体节点 (Agent Nodes) 与运行时动态模型适配
# ------------------------------------------------------------
# 每一个节点代表图状态机中的一个基本执行单元。
# 为了支持多用户及前端动态配置 API Key，我们在节点执行时通过 config["configurable"] 
# 动态加载并实例化对应的 LLM 客户端，实现彻底的 Session 级模型路由隔离。这避免了全局单例 LLM 造成的并发/密钥冲突。
# ============================================================

def get_llm_from_config(config: dict):
    """
    根据传入的运行时配置（从前端获取）动态初始化对应的 LLM 客户端。
    如果在编译图的配置中找不到，则退化至本地 .env 读取的环境变量。
    """
    cfg = config.get("configurable", {}) if config else {}
    
    provider = cfg.get("provider", "openai")
    api_key = cfg.get("api_key") or os.getenv("OPENAI_API_KEY", "")
    base_url = cfg.get("base_url") or os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    model_name = cfg.get("model_name") or os.getenv("MODEL_NAME", "gpt-4o-mini")
    temperature = cfg.get("temperature", 0.2)
    
    use_vertex = cfg.get("use_vertex", os.getenv("GOOGLE_VERTEX_AI", "false").lower() == "true")
    vertex_model = cfg.get("vertex_model_name", os.getenv("GOOGLE_VERTEX_MODEL_NAME", "gemini-2.5-flash"))
    vertex_project = cfg.get("vertex_project_id", os.getenv("GOOGLE_VERTEX_PROJECT_ID", ""))
    
    if provider == "gemini" or use_vertex:
        return ChatGoogleGenerativeAI(
            model=vertex_model,
            project=vertex_project,
            temperature=temperature,
        )
    else:
        return ChatOpenAI(
            api_key=api_key,
            base_url=base_url,
            model=model_name,
            temperature=temperature,
        )

# 1. Architect 节点
def call_architect(state: State, config: RunnableConfig):
    messages = state["messages"]
    # 确保首条消息前有 架构师 的 System 指令
    if not any(isinstance(m, SystemMessage) and "架构师" in m.content for m in messages):
        messages = [SystemMessage(content=ARCHITECT_PROMPT)] + messages
    llm = get_llm_from_config(config).bind_tools(architect_tools)
    response = llm.invoke(messages)
    return {"messages": [response]}

# 2. Developer 节点
def call_developer(state: State, config: RunnableConfig):
    messages = state["messages"]
    # 注入 Developer System 指令，保持单一系统指令干净、高效
    messages = [SystemMessage(content=DEVELOPER_PROMPT)] + [m for m in messages if not isinstance(m, SystemMessage)]
    llm = get_llm_from_config(config).bind_tools(developer_tools)
    response = llm.invoke(messages)
    return {"messages": [response]}

# 3. Reviewer 节点
def call_reviewer(state: State, config: RunnableConfig):
    messages = state["messages"]
    # 注入 Reviewer System 指令
    messages = [SystemMessage(content=REVIEWER_PROMPT)] + [m for m in messages if not isinstance(m, SystemMessage)]
    llm = get_llm_from_config(config).bind_tools(reviewer_tools)
    response = llm.invoke(messages)
    return {"messages": [response]}

# 处理重试的辅助节点
def developer_retry_node(state: State):
    retries = state.get("retry_count", 0) + 1
    # 插入一条重试提示消息，反馈给 Developer 节点
    retry_msg = HumanMessage(content=f"[SYSTEM] 审查不通过。已开启第 {retries} 次重试，请开发者根据审查反馈进行修正。")
    return {"messages": [retry_msg], "retry_count": retries}
