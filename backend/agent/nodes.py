import os

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from .credentials import runtime_credentials
from .prompts import ARCHITECT_PROMPT, DEVELOPER_PROMPT, REVIEWER_PROMPT

# 使用相对导入保证子包高内聚、易移植
from .state import State
from .tools import architect_tools, developer_tools, reviewer_tools

# ============================================================
# 教学说明: 智能体节点 (Agent Nodes) 与运行时动态模型适配
# ------------------------------------------------------------
# 每一个节点代表图状态机中的一个基本执行单元。
# 为了支持多用户及前端动态配置 API Key，我们在节点执行时通过 config["configurable"]
# 动态加载并实例化对应的 LLM 客户端，实现彻底的 Session 级模型路由隔离。这避免了全局单例 LLM 造成的并发/密钥冲突。
# ============================================================


def _get_runtime_api_key(cfg: dict, *environment_names: str) -> str:
    """从短期凭据仓库取密钥，未提供客户端密钥时再读取服务端环境变量。"""

    credential_ref = cfg.get("credential_ref")
    if credential_ref:
        return runtime_credentials.resolve(str(credential_ref))
    for environment_name in environment_names:
        api_key = os.getenv(environment_name)
        if api_key:
            return api_key
    return ""


def get_llm_from_config(config: RunnableConfig):
    """
    根据传入的运行时配置（从前端获取）动态初始化对应的 LLM 客户端。
    支持三种 Provider:
    - "openai"       : OpenAI 兼容接口 (如 GPT, 智谱, GLM 等)
    - "gemini_studio": Gemini AI Studio (Google AI Studio)
    - "google_vertex": Google Cloud Vertex AI (支持 ADC 凭证文件或 API Key 验证)
    """
    cfg = config.get("configurable", {}) if config else {}

    provider = cfg.get("provider", "openai")
    model_name = str(cfg.get("model_name") or os.getenv("MODEL_NAME", "gpt-4o-mini"))
    temperature = cfg.get("temperature", 0.2)

    if provider == "openai":
        api_key = _get_runtime_api_key(cfg, "OPENAI_API_KEY")
        base_url = str(
            cfg.get("base_url")
            or os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
        )
        return ChatOpenAI(
            api_key=SecretStr(api_key),
            base_url=base_url,
            model=model_name,
            temperature=temperature,
        )

    elif provider == "gemini_studio":
        api_key = _get_runtime_api_key(cfg, "GEMINI_API_KEY", "GOOGLE_API_KEY")
        return ChatGoogleGenerativeAI(
            model=model_name,
            google_api_key=api_key,
            vertexai=False,
            temperature=temperature,
        )

    elif provider == "google_vertex":
        project = cfg.get("vertex_project_id") or os.getenv(
            "GOOGLE_VERTEX_PROJECT_ID", ""
        )
        location = cfg.get("vertex_location") or os.getenv(
            "VERTEX_LOCATION", "us-central1"
        )
        model = cfg.get("vertex_model_name") or os.getenv(
            "GOOGLE_VERTEX_MODEL_NAME", "gemini-2.5-flash"
        )
        auth_mode = cfg.get("vertex_auth_mode", "adc")  # "adc" 或 "api_key"

        if auth_mode == "adc":
            # ADC 路径只能由后端启动环境提供，不能由单次请求改写进程级环境变量。
            # 这避免并发会话互相替换认证身份，也阻止客户端借路径参数探测本机文件。
            return ChatGoogleGenerativeAI(
                model=model,
                project=project,
                location=location,
                vertexai=True,
                temperature=temperature,
            )
        else:  # "api_key"
            api_key = _get_runtime_api_key(cfg, "VERTEX_API_KEY")
            # Vertex AI 的 API Key 属于 Express Mode；Google Gen AI 客户端明确要求
            # API Key 与 project/location 互斥。这里与模型目录连接测试保持同一鉴权语义，
            # 避免 UI 显示连接成功、真正执行 Agent 时却因参数冲突失败。
            return ChatGoogleGenerativeAI(
                model=model,
                google_api_key=api_key,
                vertexai=True,
                temperature=temperature,
            )

    else:
        # 兼容旧版的 use_vertex 退化逻辑
        use_vertex = os.getenv("GOOGLE_VERTEX_AI", "false").lower() == "true"
        if use_vertex:
            vertex_model = os.getenv("GOOGLE_VERTEX_MODEL_NAME", "gemini-2.5-flash")
            vertex_project = os.getenv("GOOGLE_VERTEX_PROJECT_ID", "")
            return ChatGoogleGenerativeAI(
                model=vertex_model,
                project=vertex_project,
                vertexai=True,
                temperature=temperature,
            )
        else:
            api_key = os.getenv("OPENAI_API_KEY", "")
            base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
            return ChatOpenAI(
                api_key=SecretStr(api_key),
                base_url=base_url,
                model=model_name,
                temperature=temperature,
            )


# 1. Architect 节点
def call_architect(state: State, config: RunnableConfig):
    messages = state["messages"]
    # 确保首条消息前有 架构师 的 System 指令
    if not any(
        isinstance(m, SystemMessage) and "架构师" in m.content for m in messages
    ):
        messages = [SystemMessage(content=ARCHITECT_PROMPT)] + messages
    llm = get_llm_from_config(config).bind_tools(architect_tools)
    response = llm.invoke(messages)
    return {"messages": [response]}


# 2. Developer 节点
def call_developer(state: State, config: RunnableConfig):
    messages = state["messages"]
    # 注入 Developer System 指令，保持单一系统指令干净、高效
    messages = [SystemMessage(content=DEVELOPER_PROMPT)] + [
        m for m in messages if not isinstance(m, SystemMessage)
    ]
    llm = get_llm_from_config(config).bind_tools(developer_tools)
    response = llm.invoke(messages)
    return {"messages": [response]}


# 3. Reviewer 节点
def call_reviewer(state: State, config: RunnableConfig):
    messages = state["messages"]
    # 注入 Reviewer System 指令
    messages = [SystemMessage(content=REVIEWER_PROMPT)] + [
        m for m in messages if not isinstance(m, SystemMessage)
    ]
    llm = get_llm_from_config(config).bind_tools(reviewer_tools)
    response = llm.invoke(messages)
    return {"messages": [response]}


# 处理重试的辅助节点
def developer_retry_node(state: State):
    retries = state.get("retry_count", 0) + 1
    # 插入一条重试提示消息，反馈给 Developer 节点
    retry_msg = HumanMessage(
        content=f"[SYSTEM] 审查不通过。已开启第 {retries} 次重试，请开发者根据审查反馈进行修正。"
    )
    return {"messages": [retry_msg], "retry_count": retries}
