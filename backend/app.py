import asyncio
import json
from typing import Dict

import uvicorn
from agent_core import simple_refactor, stream_refactor
from code_indexer import index_directory
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from graph_indexer import get_topology_data, index_to_neo4j
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
                print(f"[WebSocket] Sent message to thread `{thread_id}`: {message.get('type')}")
            except Exception as e:
                print(f"[WebSocket] Failed to send message to thread `{thread_id}`: {e}")

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


@app.on_event("startup")
def startup_event():
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


@app.get("/")
def read_root():
    return {"message": "Welcome to Refactor-Agent API. The service is running!"}


@app.post("/api/refactor", response_model=RefactorResponse)
def refactor_code(request: RefactorRequest):
    """
    接收代码，调用 Agent 进行简单重构
    """
    config = {"configurable": {"thread_id": request.thread_id}}
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
    config = {"configurable": {"thread_id": request.thread_id}}
    if request.custom_model_config:
        config["configurable"].update(request.custom_model_config)

    # 阶段 2：检测是否需要恢复已被挂起的执行
    if request.thread_id in session_approvals:
        approved = session_approvals.pop(request.thread_id)
        config["configurable"]["resume_value"] = {"approved": approved}
        print(f"[app] Resuming graph for thread `{request.thread_id}` with approved={approved}")

    def event_generator():
        for token in stream_refactor(request.code, request.thread_id, config):
            # 将每个 token 序列化为 JSON 以便前端解析
            yield f"data: {json.dumps({'token': token})}\n\n"
            
        # 阶段 2：执行完毕后，检查是否产生了新的挂起（Interrupt）
        from agent import app_graph
        try:
            state = app_graph.get_state(config)
            if state.interrupts:
                # 存在挂起中断（即 write_code_file 工具调用前暂停）
                interrupt_payload = state.interrupts[0].value
                print(f"[app] Graph suspended on interrupt for `{request.thread_id}`. Sending WS message.")
                
                # 线程安全地异步提交发送 WebSocket 消息给主事件循环
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    asyncio.run_coroutine_threadsafe(
                        manager.send_personal_message({
                            "type": "approval_request",
                            "payload": interrupt_payload
                        }, request.thread_id),
                        loop
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


if __name__ == "__main__":
    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=True)
