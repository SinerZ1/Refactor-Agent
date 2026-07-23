import asyncio
import json
import os
from typing import Dict, Literal

import uvicorn
from agent_core import simple_refactor, stream_refactor
from code_indexer import index_directory
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from graph_indexer import get_topology_data, index_to_neo4j
from model_catalog import (
    ModelCatalogError,
    ModelConnectionConfig,
    inspect_adc_file,
    list_available_models,
)
from pydantic import BaseModel, ConfigDict, Field

app = FastAPI(title="Refactor-Agent Backend")

session_approvals: Dict[str, bool] = {}


class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}

    async def connect(self, websocket: WebSocket, thread_id: str):
        await websocket.accept()
        self.active_connections[thread_id] = websocket
        print(f"[WebSocket] Thread `{thread_id}` connected.")

    def disconnect(self, thread_id: str):
        if thread_id in self.active_connections:
            del self.active_connections[thread_id]
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
    await manager.connect(websocket, thread_id)
    try:
        while True:
            data = await websocket.receive_json()
            print(f"[WebSocket] Received message from thread `{thread_id}`: {data}")

            # 阶段 2: 处理审批放行/拒绝信号
            if data.get("type") == "approval_response":
                approved = data.get("approved", False)
                session_approvals[thread_id] = approved
                await websocket.send_json({"type": "approval_confirmed"})
                print(f"[WebSocket] Approval saved for `{thread_id}`: {approved}")

    except WebSocketDisconnect:
        manager.disconnect(thread_id)
    except Exception as e:
        print(f"[WebSocket Error] Thread `{thread_id}`: {e}")
        manager.disconnect(thread_id)


async def send_chatroom_message(thread_id: str, sender: str, content: str):
    """
    阶段 3: A2A 多角色聊天室，将智能体的中间发言向 WebSocket 广播
    """
    await manager.send_personal_message(
        {"type": "chatroom_message", "sender": sender, "content": content}, thread_id
    )


main_loop = None


@app.on_event("startup")
def startup_event():
    global main_loop
    main_loop = asyncio.get_event_loop()
    # 启动时自动静态扫描 CodeSmells 目录，构建 AST 符号索引
    index_directory()
    # 启动时同时将代码库关系索引至 Neo4j 中
    try:
        index_to_neo4j()
    except Exception as e:
        print(
            f"[Startup] Neo4j 初始化图索引失败 (若未启动 Neo4j 服务请忽略，系统支持降级运行): {e}"
        )


# 配置 CORS，允许前端应用访问
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 允许所有源（开发环境方便调试）
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
class RefactorRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    code: str
    thread_id: str = "default_session"
    custom_model_config: dict | None = Field(default=None, alias="model_config")


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


@app.get("/")
def read_root():
    return {"message": "Welcome to Refactor-Agent API. The service is running!"}


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
    config: dict[str, dict[str, object]] = {
        "configurable": {
            "thread_id": request.thread_id,
        }
    }
    if request.custom_model_config:
        config["configurable"].update(request.custom_model_config)
    refactored_result = simple_refactor(request.code, config)

    return RefactorResponse(
        original_code=request.code, refactored_code=refactored_result
    )


@app.post("/api/refactor/stream")
def refactor_code_stream(request: RefactorRequest):
    """
    流式接收重构代码，返回 SSE (Server-Sent Events) 流
    """
    config: dict[str, dict[str, object]] = {
        "configurable": {
            "thread_id": request.thread_id,
        }
    }
    if request.custom_model_config:
        config["configurable"].update(request.custom_model_config)

    # 阶段 2：检测是否需要恢复已被挂起的执行
    if request.thread_id in session_approvals:
        approved = session_approvals.pop(request.thread_id)
        config["configurable"]["resume_value"] = {"approved": approved}
        print(
            f"[app] Resuming graph for thread `{request.thread_id}` with approved={approved}"
        )

    def event_generator():
        for token in stream_refactor(
            request.code, request.thread_id, config, send_chatroom_message, main_loop
        ):
            # 将每个 token 序列化为 JSON 以便前端解析
            yield f"data: {json.dumps({'token': token})}\n\n"

        # 阶段 2：执行完毕后，检查是否产生了新的挂起（Interrupt）
        from agent import app_graph

        try:
            state = app_graph.get_state(config)
            if state.interrupts:
                # 存在挂起中断（即 write_code_file 工具调用前暂停）
                interrupt_payload = state.interrupts[0].value
                print(
                    f"[app] Graph suspended on interrupt for `{request.thread_id}`. Sending WS message and SSE token."
                )

                # 方案 A：通过 SSE 确保前端必定收到
                yield f"data: {json.dumps({'token': '[APPROVAL_REQUEST]' + json.dumps(interrupt_payload)})}\n\n"

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
        except Exception as e:
            print(f"[app] Failed to check state interrupts: {e}")

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
    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=True)
