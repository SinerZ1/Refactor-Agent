# Refactor-Agent

## 项目定位

Refactor-Agent 是一个面向 Python 历史代码教学与实验的多智能体重构工作台。它以
LangGraph 编排 `Architect → Developer → Reviewer` 主流程，通过浏览器或桌面界面
展示推理事件、工具执行、代码 Diff、人工审批和代码依赖图。Agent 的文件读写范围
被限制在 `CodeSmells/`，写入前必须经过人工确认。

## 当前功能

- Architect 读取代码、检索 AST 符号并查询 Neo4j/内存调用图，生成重构方案。
- Developer 在 `CodeSmells/` 内读写 Python 文件；每次写入通过 HITL Diff 弹窗等待审批。
- Reviewer 仅能运行预定义测试套件；成功终态要求当前变更摘要对应的 CodeSmells
  测试通过，并同时满足结构化证据门禁和成功协议。
- FastAPI 以 SSE 流式推送版本化 Agent 事件，以 WebSocket 承载 A2A 消息和审批交互。
- Redis 可持久化 LangGraph checkpoint；不可用时自动降级为进程内 `MemorySaver`。
- Neo4j 可保存代码调用拓扑；不可用时自动降级为内存 AST 调用图。
- Vue Flow 展示 Architect 计划及运行事件映射出的任务状态，ECharts 展示代码依赖拓扑。
- 支持 OpenAI 兼容接口、Gemini API 和 Google Vertex AI（ADC 或 API Key）运行时配置。

## 技术栈

| 层级 | 主要技术 |
| --- | --- |
| Agent 与后端 | Python 3.12、FastAPI、LangGraph、LangChain、Pydantic |
| 模型适配 | OpenAI 兼容 API、Gemini API、Google Vertex AI |
| 状态与图数据 | Redis、Neo4j、Python AST 内存降级 |
| 前端 | Vue 3、TypeScript、Vite、Vue Flow、ECharts |
| 桌面端 | Pywebview、Uvicorn、FastAPI 静态资源托管 |
| 测试 | Pytest、Vitest、Vue Test Utils、Playwright |

## 目录结构

```text
Refactor-Agent/
├─ CodeSmells/             # Agent 被允许读取和重构的示例历史代码
├─ backend/
│  ├─ agent/               # 状态、提示词、工具、节点、路由与工作流编排
│  ├─ evals/               # 24 个 Agent 场景、离线基准与 JSON/Markdown 报告
│  ├─ scripts/             # 虚拟环境中的开发命令入口
│  ├─ tests/               # 后端测试
│  ├─ app.py               # FastAPI、SSE、WebSocket 与静态资源入口
│  └─ main_gui.py          # Pywebview 桌面入口
├─ frontend/
│  ├─ src/                 # Vue 应用、组件、composables 与前端测试
│  └─ e2e/                 # Playwright 端到端测试
└─ docs/
   └─ architecture.md      # 通信、HITL、存储及降级架构
```

## 本地启动

### 1. 准备环境

需要 Windows 10/11、Python 3.12 和 Node.js `^22.18.0` 或 `>=24.12.0`。
Redis 与 Neo4j 是可选项：二者未启动时，应用仍可通过内存降级模式运行。

首次克隆后，在 PowerShell 中初始化后端。创建虚拟环境是唯一尚不能使用
`venv/Scripts/` 的引导命令；之后所有后端命令都使用仓库内虚拟环境。

```powershell
cd backend
py -3.12 -m venv venv
venv\Scripts\python.exe -m pip install -r requirements-dev.txt
Copy-Item .env.example .env
```

`backend/.env` 默认即可启动服务。真正执行模型重构前，还需要在网页中填写模型
API Key，或在 `.env` 中配置一种模型凭据。不要提交 `.env`、ADC 文件或任何真实密钥。
OpenAI 兼容服务（例如 DeepSeek 或智谱）使用 `OPENAI_BASE_URL`、`OPENAI_API_KEY`
和 `MODEL_NAME`。

安装前端依赖：

```powershell
cd ..\frontend
npm.cmd ci
```

### 2. 启动 Web 开发环境

打开两个 PowerShell 窗口。

后端窗口：

```powershell
cd backend
venv\Scripts\python.exe app.py
```

前端窗口：

```powershell
cd frontend
npm.cmd run dev
```

浏览器访问 `http://127.0.0.1:5173`。默认前端会连接
`http://127.0.0.1:8000`；若修改后端地址，可在前端运行环境中设置
`VITE_API_BASE_URL`。

### 3. 启动桌面应用（可选）

桌面入口使用 FastAPI 同源托管已构建的 Vue 产物，不需要 Vite 开发服务器：

```powershell
cd frontend
npm.cmd run build

cd ..\backend
venv\Scripts\python.exe main_gui.py
```

### 4. 可选基础设施

- Redis：设置 `REDIS_URL` 后启用 checkpoint 持久化；连接失败会回退到 `MemorySaver`。
- Neo4j：设置 `NEO4J_URI`、`NEO4J_USER`、`NEO4J_PASSWORD` 后启用持久化调用图；
  连接失败会回退到内存 AST 调用图。

完整数据流和降级关系见 [架构说明](docs/architecture.md)。

## Agent 评测

在 `backend/` 目录运行一条命令即可执行不调用付费 API 的离线评测：

```powershell
venv\Scripts\python.exe -m evals.run --mode offline
```

评测覆盖长函数、重复代码、命名、高耦合、类型、异常、跨文件依赖、Prompt
injection、越界写入、审批拒绝、测试失败和预算耗尽，输出 JSON 与 Markdown 报告，
并统计行为测试、审查、重试、工具、Token、耗时、HITL 和安全拦截指标。报告目录
`backend/evals/output/` 不进入版本控制；仓库只保留不含 Prompt、API 响应和凭据的稳定
离线基准。可选真实模型模式及数据边界见
[`backend/evals/README.md`](backend/evals/README.md)。

## 当前限制

- 动态任务 DAG 当前是**计划可视化和事件状态看板**，尚未按任务节点和依赖关系逐项调度；
  实际执行仍遵循单一的 `Architect → Developer → Reviewer` LangGraph 主流程。
- 会话注册表和运行时 API Key 保存在当前进程内；服务重启后需要重新建立会话和模型配置。
- 未配置 Redis 时，HITL checkpoint 只在当前进程内有效；未配置 Neo4j 时只提供 AST 降级图。
- Agent 消息 Markdown 尚未完成系统性净化，当前版本应仅用于可信本地开发环境。
- 当前仓库尚未配置完整 CI，阶段 0 的本地验证基线（2026-07-28）为：
  后端 `67 passed`，前端 `17 passed`，前端类型检查和生产构建均通过。
