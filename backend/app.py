from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
import uvicorn
import json
from agent_core import simple_refactor, stream_refactor
from code_indexer import index_directory

app = FastAPI(title="Refactor-Agent Backend")

@app.on_event("startup")
def startup_event():
    # 启动时自动静态扫描 CodeSmells 目录，构建 AST 符号索引
    index_directory()

# 配置 CORS，允许前端应用（如 Vite 默认端口 5173）访问
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 定义前端请求的数据模型
class RefactorRequest(BaseModel):
    code: str
    thread_id: str = "default_session"

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
    refactored_result = simple_refactor(request.code)
    
    return RefactorResponse(
        original_code=request.code,
        refactored_code=refactored_result
    )

@app.post("/api/refactor/stream")
def refactor_code_stream(request: RefactorRequest):
    """
    流式接收重构代码，返回 SSE (Server-Sent Events) 流
    """
    def event_generator():
        for token in stream_refactor(request.code, request.thread_id):
            # 将每个 token 序列化为 JSON 以便前端解析
            yield f"data: {json.dumps({'token': token})}\n\n"
    
    return StreamingResponse(event_generator(), media_type="text/event-stream")

if __name__ == "__main__":
    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=True)
