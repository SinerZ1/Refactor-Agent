from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
import uvicorn
import json
from agent_core import simple_refactor, stream_refactor
from code_indexer import index_directory
from graph_indexer import index_to_neo4j, get_topology_data

app = FastAPI(title="Refactor-Agent Backend")

@app.on_event("startup")
def startup_event():
    # 启动时自动静态扫描 CodeSmells 目录，构建 AST 符号索引
    index_directory()
    # 启动时同时将代码库关系索引至 Neo4j 中
    try:
        index_to_neo4j()
    except Exception as e:
        print(f"[Startup] Neo4j 初始化图索引失败 (若未启动 Neo4j 服务请忽略，系统支持降级运行): {e}")

# 配置 CORS，允许前端应用访问
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # 允许所有源（开发环境方便调试）
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 定义前端请求的数据模型
class RefactorRequest(BaseModel):
    code: str
    thread_id: str = "default_session"
    model_config: dict = None

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
    if request.model_config:
        config["configurable"].update(request.model_config)
    refactored_result = simple_refactor(request.code, config)
    
    return RefactorResponse(
        original_code=request.code,
        refactored_code=refactored_result
    )

@app.post("/api/refactor/stream")
def refactor_code_stream(request: RefactorRequest):
    """
    流式接收重构代码，返回 SSE (Server-Sent Events) 流
    """
    config = {"configurable": {"thread_id": request.thread_id}}
    if request.model_config:
        config["configurable"].update(request.model_config)

    def event_generator():
        for token in stream_refactor(request.code, request.thread_id, config):
            # 将每个 token 序列化为 JSON 以便前端解析
            yield f"data: {json.dumps({'token': token})}\n\n"
    
    return StreamingResponse(event_generator(), media_type="text/event-stream")

@app.get("/api/graph/topology")
def get_graph_topology():
    """
    获取目前代码库的调用关系图拓扑数据，提供给前端可视化组件
    """
    return get_topology_data()

if __name__ == "__main__":
    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=True)
