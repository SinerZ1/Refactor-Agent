import asyncio
import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal
from urllib.parse import urlsplit

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from agent.credentials import runtime_credentials
from agent.events import make_agent_event
from agent_core import simple_refactor, stream_refactor
from code_indexer import index_directory
from graph_indexer import get_topology_data, index_to_neo4j
from model_catalog import (
    ModelCatalogError,
    ModelConnectionConfig,
    inspect_adc_file,
    list_available_models,
)
from session_registry import (
    ApprovalStateError,
    SessionAuthorizationError,
    runtime_sessions,
)

# 保证服务入口最早加载环境变量，便于状态图 Checkpointer 等子模块初始化
load_dotenv()

main_loop: asyncio.AbstractEventLoop | None = None


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """在 ASGI 生命周期中构建索引，并保存真正运行服务的事件循环。"""

    global main_loop
    main_loop = asyncio.get_running_loop()
    index_directory()
    try:
        index_to_neo4j()
    except Exception as exc:
        print(
            "[Startup] Neo4j 初始化图索引失败 "
            f"(若未启动 Neo4j 服务请忽略，系统支持降级运行): {exc}"
        )
    try:
        yield
    finally:
        main_loop = None


app = FastAPI(title="Refactor-Agent Backend", lifespan=lifespan)

DEFAULT_ALLOWED_ORIGINS = {
    "http://127.0.0.1:5173",
    "http://localhost:5173",
    "http://127.0.0.1:8000",
    "http://localhost:8000",
}
ALLOWED_ORIGINS = {
    origin.strip()
    for origin in os.getenv(
        "ALLOWED_ORIGINS", ",".join(sorted(DEFAULT_ALLOWED_ORIGINS))
    ).split(",")
    if origin.strip()
}


def is_websocket_origin_allowed(origin: str | None, host: str) -> bool:
    """允许配置白名单或动态端口下的同源回环连接。"""

    if not origin:
        return True
    if origin in ALLOWED_ORIGINS:
        return True
    parsed = urlsplit(origin)
    return (
        parsed.scheme in {"http", "https"}
        and parsed.netloc == host
        and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    )


class ConnectionManager:
    def __init__(self):
        self.active_connections: dict[str, WebSocket] = {}

    async def connect(self, websocket: WebSocket, thread_id: str):
        await websocket.accept()
        previous = self.active_connections.get(thread_id)
        if previous and previous is not websocket:
            await previous.close(code=1012, reason="会话已在新连接中恢复")
        self.active_connections[thread_id] = websocket
        print(f"[WebSocket] Thread `{thread_id}` connected.")

    def disconnect(self, thread_id: str, websocket: WebSocket):
        if self.active_connections.get(thread_id) is websocket:
            self.active_connections.pop(thread_id, None)
            print(f"[WebSocket] Thread `{thread_id}` disconnected.")

    async def send_personal_message(self, message: dict, thread_id: str):
        if thread_id in self.active_connections:
            try:
                await self.active_connections[thread_id].send_json(message)
                print(
                    f"[WebSocket] Sent message to thread `{thread_id}`: {message.get('type')}"
                )
            except Exception as e:
                print(
                    f"[WebSocket] Failed to send message to thread `{thread_id}`: {e}"
                )


manager = ConnectionManager()


@app.websocket("/ws/refactor/{thread_id}")
async def websocket_endpoint(websocket: WebSocket, thread_id: str):
    origin = websocket.headers.get("origin")
    session_token = websocket.query_params.get("token", "")
    if not is_websocket_origin_allowed(origin, websocket.headers.get("host", "")):
        await websocket.close(code=1008, reason="Origin 不在允许列表")
        return
    try:
        runtime_sessions.require(thread_id, session_token)
    except SessionAuthorizationError:
        await websocket.close(code=1008, reason="会话认证失败")
        return

    await manager.connect(websocket, thread_id)
    try:
        while True:
            data = await websocket.receive_json()

            # 阶段 2: 处理审批放行/拒绝信号
            if data.get("type") == "approval_response":
                try:
                    runtime_sessions.decide(
                        thread_id,
                        session_token,
                        str(data.get("approval_id", "")),
                        bool(data.get("approved", False)),
                    )
                except ApprovalStateError as exc:
                    await websocket.send_json(
                        {"type": "approval_error", "message": str(exc)}
                    )
                    continue
                await websocket.send_json(
                    {
                        "type": "approval_confirmed",
                        "approval_id": data.get("approval_id"),
                    }
                )

    except WebSocketDisconnect:
        manager.disconnect(thread_id, websocket)
    except Exception as e:
        print(f"[WebSocket Error] Thread `{thread_id}`: {e}")
        manager.disconnect(thread_id, websocket)


async def send_chatroom_message(thread_id: str, sender: str, content: str):
    """
    阶段 3: A2A 多角色聊天室，将智能体的中间发言向 WebSocket 广播
    """
    await manager.send_personal_message(
        {"type": "chatroom_message", "sender": sender, "content": content}, thread_id
    )


# 配置 CORS，允许前端应用访问
app.add_middleware(
    CORSMiddleware,
    allow_origins=sorted(ALLOWED_ORIGINS),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# 定义前端请求的数据模型
# ============================================================
# 【教学与理论关联 - 接口抗脆弱性与平滑升级】
# 在从 Pydantic v1 升级至 v2 的实践中，`model_config` 被提升为类保留属性（用于配置模型本身）。
# 为保证前端 API 接口的不破坏性（Backward Compatibility），我们不改变前端传入的 JSON 字段名 `model_config`。
# 而是使用 Pydantic v2 提供的 `Field(alias="...")` 机制进行别名映射，使字段在 Python 内部表示为 `custom_model_config`。
# 配合 `populate_by_name=True`，允许 Python 代码中无论使用属性名还是别名，均能正常加载与解析。
# ============================================================
class RuntimeModelConfig(BaseModel):
    """进入 Agent 的模型配置；密钥会在构图前迁移到进程内凭据仓库。"""

    model_config = ConfigDict(extra="forbid")

    provider: Literal["openai", "gemini_studio", "google_vertex"] = "openai"
    api_key: SecretStr = SecretStr("")
    base_url: str = ""
    model_name: str = ""
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    vertex_project_id: str = ""
    vertex_location: str = "global"
    vertex_model_name: str = ""
    vertex_auth_mode: Literal["adc", "api_key"] = "adc"


class RefactorRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    code: str
    thread_id: str = Field(
        default="default_session",
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:-]+$",
    )
    custom_model_config: RuntimeModelConfig | None = Field(
        default=None, alias="model_config"
    )
    session_token: SecretStr = SecretStr("")


# 定义返回的数据模型
class RefactorResponse(BaseModel):
    original_code: str
    refactored_code: str


class ModelConnectionRequest(BaseModel):
    """连接测试的最小配置，不写日志、不落盘，也不进入 Agent Graph State。"""

    provider: Literal["openai", "gemini_studio", "google_vertex"]
    api_key: str = ""
    base_url: str = ""
    project_id: str = ""
    location: str = "global"
    auth_mode: Literal["adc", "api_key"] = "adc"


class ModelConnectionResponse(BaseModel):
    models: list[str]
    message: str


class SessionResponse(BaseModel):
    thread_id: str
    session_token: str


class ApprovalDecisionRequest(BaseModel):
    session_token: SecretStr
    approval_id: str = Field(min_length=1, max_length=128)
    approved: bool


class ApprovalDecisionResponse(BaseModel):
    approval_id: str
    accepted: bool = True


def build_graph_config(
    request: RefactorRequest,
) -> tuple[RunnableConfig, str | None]:
    """构造可持久化的图配置，并把真正密钥替换成随机引用。"""

    configurable: dict[str, object] = {"thread_id": request.thread_id}
    credential_ref: str | None = None
    if request.custom_model_config:
        model_settings = request.custom_model_config
        configurable.update(model_settings.model_dump(exclude={"api_key"}))
        credential_ref = runtime_credentials.store(
            model_settings.api_key.get_secret_value()
        )
        if credential_ref:
            configurable["credential_ref"] = credential_ref
    return {"configurable": configurable}, credential_ref


def authorize_refactor_request(request: RefactorRequest) -> str:
    session_token = request.session_token.get_secret_value()
    try:
        runtime_sessions.require(request.thread_id, session_token)
    except SessionAuthorizationError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    return session_token


@app.get("/api")
def read_root():
    return {"message": "Welcome to Refactor-Agent API. The service is running!"}


@app.get("/api/health")
def health_check():
    return {"status": "ok"}


@app.post("/api/sessions", response_model=SessionResponse)
def create_session():
    credentials = runtime_sessions.create()
    return SessionResponse(
        thread_id=credentials.thread_id,
        session_token=credentials.session_token,
    )


@app.post(
    "/api/sessions/{thread_id}/approval",
    response_model=ApprovalDecisionResponse,
)
def submit_approval(thread_id: str, request: ApprovalDecisionRequest):
    try:
        runtime_sessions.decide(
            thread_id,
            request.session_token.get_secret_value(),
            request.approval_id,
            request.approved,
        )
    except SessionAuthorizationError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except ApprovalStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return ApprovalDecisionResponse(approval_id=request.approval_id)


@app.get("/api/models/adc-status")
def get_adc_status():
    """返回脱敏后的 ADC 探测结果，绝不把本机凭据路径发送到浏览器。"""

    return inspect_adc_file().public_dict()


@app.post("/api/models/connect", response_model=ModelConnectionResponse)
async def connect_model_provider(request: ModelConnectionRequest):
    """校验供应商配置并归一化返回可用模型目录。"""

    try:
        models = await list_available_models(
            ModelConnectionConfig(**request.model_dump())
        )
    except ModelCatalogError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ModelConnectionResponse(
        models=models, message=f"连接成功，共发现 {len(models)} 个模型"
    )


@app.post("/api/refactor", response_model=RefactorResponse)
def refactor_code(request: RefactorRequest):
    """
    接收代码，调用 Agent 进行简单重构
    """
    authorize_refactor_request(request)
    config, credential_ref = build_graph_config(request)
    try:
        refactored_result = simple_refactor(request.code, config)
    finally:
        runtime_credentials.revoke(credential_ref)

    return RefactorResponse(
        original_code=request.code, refactored_code=refactored_result
    )


@app.post("/api/refactor/stream")
def refactor_code_stream(request: RefactorRequest):
    """
    流式接收重构代码，返回 SSE (Server-Sent Events) 流
    """
    session_token = authorize_refactor_request(request)
    config, credential_ref = build_graph_config(request)

    # 阶段 2：检测是否需要恢复已被挂起的执行
    approval_decision = runtime_sessions.consume_decision(
        request.thread_id, session_token
    )
    if approval_decision:
        _, approved = approval_decision
        config["configurable"]["resume_value"] = {"approved": approved}
        print(
            f"[app] Resuming graph for thread `{request.thread_id}` with approved={approved}"
        )

    def event_generator():
        stream_failed = False
        try:
            for event in stream_refactor(
                request.code,
                request.thread_id,
                config,
                send_chatroom_message,
                main_loop,
            ):
                # SSE 直接承载版本化事件；事件内保留 token 仅用于旧前端显示兼容。
                stream_failed = stream_failed or event["type"] == "run.failed"
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

            # 阶段 2：执行完毕后，检查是否产生了新的挂起（Interrupt）
            from agent import app_graph

            try:
                state = app_graph.get_state(config)
                if state.interrupts:
                    # 存在挂起中断（即 write_code_file 工具调用前暂停）
                    interrupt_payload = dict(state.interrupts[0].value)
                    interrupt_payload["approval_id"] = runtime_sessions.begin_approval(
                        request.thread_id, session_token
                    )
                    print(
                        f"[app] Graph suspended on interrupt for `{request.thread_id}`. Sending WS message and SSE token."
                    )

                    # 方案 A：通过 SSE 确保前端必定收到
                    approval_event = make_agent_event(
                        "approval.waiting",
                        "等待用户确认文件写入",
                        node="developer",
                        tool="write_code_file",
                        payload=interrupt_payload,
                    )
                    yield f"data: {json.dumps(approval_event, ensure_ascii=False)}\n\n"

                    # 方案 B：同时尝试通过 WebSocket 发送（向下兼容）
                    try:
                        loop = main_loop
                        if loop and loop.is_running():
                            asyncio.run_coroutine_threadsafe(
                                manager.send_personal_message(
                                    {
                                        "type": "approval_request",
                                        "payload": interrupt_payload,
                                    },
                                    request.thread_id,
                                ),
                                loop,
                            )
                    except Exception as ex:
                        print(
                            f"[app] ERROR: Cannot send WS message using run_coroutine_threadsafe: {ex}"
                        )
                else:
                    print(
                        f"[app] No interrupts found for thread `{request.thread_id}` after stream_refactor."
                    )
                    review_status = state.values.get("review_status")
                    if review_status == "success" and not stream_failed:
                        terminal_event = make_agent_event(
                            "run.completed",
                            "重构工作流已完成",
                            level="success",
                            node="workflow",
                            success=True,
                        )
                        yield f"data: {json.dumps(terminal_event, ensure_ascii=False)}\n\n"
                    elif review_status == "failed" and not stream_failed:
                        terminal_event = make_agent_event(
                            "run.failed",
                            "重构工作流未通过最终审查",
                            level="error",
                            node="workflow",
                            success=False,
                        )
                        yield f"data: {json.dumps(terminal_event, ensure_ascii=False)}\n\n"
            except Exception as e:
                print(f"[app] Failed to check state interrupts: {e}")
        finally:
            # 客户端断开时生成器也会进入 finally，避免凭据在内存中滞留到 TTL。
            runtime_credentials.revoke(credential_ref)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.get("/api/graph/topology")
def get_graph_topology():
    """
    获取目前代码库的调用关系图拓扑数据，提供给前端可视化组件
    """
    return get_topology_data()


# ============================================================
# 【教学与理论关联 - 静态资源网关与 SPA 降级代理】
# 托载本地编译后的 Vue 3 前端静态产物。
# 采用 FastAPI StaticFiles 托管，如果检测到对应 dist 目录存在，则自动启动。
# 这使得桌面端应用可以通过 Pywebview 容器直连本地服务，极大地简化了部署与人机协作。
# ============================================================
current_dir = os.path.dirname(os.path.abspath(__file__))
frontend_dist_dir = os.path.abspath(os.path.join(current_dir, "..", "frontend", "dist"))

if os.path.exists(frontend_dist_dir):
    app.mount("/", StaticFiles(directory=frontend_dist_dir, html=True), name="static")
    print(f"[Startup] Frontend static assets mounted from {frontend_dist_dir}")
else:
    print(
        f"[Startup] Warning: Frontend static directory {frontend_dist_dir} not found. Please build frontend first."
    )


if __name__ == "__main__":
    uvicorn.run(
        "app:app",
        host=os.getenv("BACKEND_HOST", "127.0.0.1"),
        port=int(os.getenv("BACKEND_PORT", "8000")),
        reload=os.getenv("BACKEND_RELOAD", "true").lower() == "true",
    )
