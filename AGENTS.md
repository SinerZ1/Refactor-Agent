# OpenCode Agent 指南 - Refactor-Agent

## 1. 软件/硬件系统环境 (System Environment)
- **操作系统**: Windows 10/11 (`win32`)
- **执行 Shell**: PowerShell 5.1 (在执行终端命令时注意 Windows 路径分隔符 `\` 与命令行差异)
- **Python 环境**: Python `3.12.10`，使用虚拟环境 `backend/venv/`
- **NodeJS 环境**: Node.js `v24.17.0`，使用 `npm` 进行包管理
- **Docker 容器**: Docker version `29.5.3`。
  - 在 Docker 中部署了 **Redis server v=8.8.0**，容器名称为 `my-redis`
  - 进入命令行终端命令: `docker exec -it my-redis redis-cli`
- **Neo4j 数据库**: Neo4j Desktop 2。
  - 本地实例名称: `Cyber-Refactor-Agent-Database` (Version: 2026.05.0)
  - 连接 URI: `neo4j://127.0.0.1:7687` (或 `bolt://127.0.0.1:7687`)
  - 认证信息: Username=`neo4j`，Password=`Siner5920`
- **目录结构**:
  - `CodeSmells/`: 存放待重构的 Python 历史代码（包含故意设计的坏味道代码）。
  - `backend/`: FastAPI 后端与 LangGraph 多智能体引擎。
  - `frontend/`: Vue 3 + Vite + TypeScript 前端。

---

## 2. 后端开发环境与快捷命令
所有后端命令必须在 `backend/` 目录下执行，且必须使用虚拟环境（venv）路径：
- **Python 解释器**: `backend/venv/Scripts/python.exe`
- **启动服务**: `backend/venv/Scripts/python.exe app.py` (运行在 `http://127.0.0.1:8000`)
- **虚拟环境内置脚本** (位于 `backend/venv/Scripts/`，在 Windows 下直接运行)：
  - 代码格式化/导入排序: `backend/venv/Scripts/format.exe`, `backend/venv/Scripts/sort-imports.exe`
  - 格式与类型检查: `backend/venv/Scripts/check-format.exe`, `backend/venv/Scripts/check-sort-imports.exe`, `backend/venv/Scripts/check-mypy.exe`, `backend/venv/Scripts/check-lint.exe`
  - 运行单元测试: `backend/venv/Scripts/test.exe` (或 `test-verbose.exe`, `backend/venv/Scripts/test-coverage.exe`)
  - 扫描死代码: `backend/venv/Scripts/find-dead-code.exe`

---

## 3. 数据库与 RAG 索引降级机制
- **静态 AST 解析**: 后端在启动时会自动扫描 `CodeSmells/`，生成 AST 符号索引（`SYMBOL_INDEX`）。
- **Neo4j 数据库**: 后端会尝试将依赖关系写入 Neo4j (`bolt://localhost:7687`)。**特别注意**: 如果 Neo4j 服务未启动，系统会自动优雅降级为**内存 AST 调用图模式**，不会导致崩溃。
- **Redis 记忆持久化**: 优先读取 Docker 容器中的 Redis，若检测到环境变量 `REDIS_URL` 则加载 `RedisSaver`，否则优雅降级为 `MemorySaver`。

---

## 4. 前端开发环境与命令
所有前端命令必须在 `frontend/` 目录下运行，使用 `npm` 进行包管理：
- **启动开发服务器**: `npm run dev` (默认端口 `5173`)
- **类型检查**: `npm run type-check` (基于 `vue-tsc --build`)
- **前端 Lint**: `npm run lint` (并行运行 `oxlint . --fix` 与 `eslint . --fix --cache`)
- **前端格式化**: `npm run format` (基于 `prettier --write`)
- **单元测试**: `npm run test:unit` (基于 Vitest)
- **E2E 测试**: `npm run test:e2e` (基于 Playwright；在 CI 运行时需先执行 `npm run build`)

---

## 5. 前后端分离与 Agent 通信架构
- **流式 SSE (Server-Sent Events) 单向流**:
  - **用途**: 用来把 Agent 的 Thought（思考过程）、Tool Call（终端执行日志等）像打字机一样实时流式推送到 Vue 3 前端。
- **双向 WebSocket 全双工**:
  - **用途**: 用于承载多智能体（A2A）的聊天室互动（如 CoderAgent 和 ReviewerAgent 甩锅讨论），以及在敏感/危险操作前（如 `write_code_file`），让 Agent 挂起并向前端发起 “人机协作审批（HITL）” 弹窗。用户在 Vue 界面点击“批准”后，信号通过 WebSocket 传回后端放行。
- **动态多模型兼容**:
  - 支持智谱、GPT、Gemini、GLM 等 API 兼容格式。模型名称、Base URL、API KEY 等配置从前端获取，后端需要动态解析、适配和加载，不能写死。

---

## 6. LangGraph 多智能体协同约束与代码实现要求 (核心)
在修改或开发 `backend/agent_core.py` 时，必须严格遵循以下规则：

### A. 工作流与状态机设计
- **工作流节点**: `Architect` (架构师) -> `Developer` (开发者) -> `Reviewer` (审查者)。
- **审查反馈流转条件 (Reviewer Routing)**:
  - 审查失败时，Reviewer 的回复内容 **必须** 包含 `【REFACTOR_FAIL】` 字符串，以此触发条件路由，退回至 `developer_retry` 节点（最多重试 3 次）。
  - 审查成功时，Reviewer 的回复内容 **必须** 包含 `【REFACTOR_SUCCESS】` 结束流程。
- **智能体工具权限限制**:
  - `Architect`: 绑定 `read_code_file`, `search_symbol_definition`, `query_neo4j_topology`。**严禁绑定任何写文件工具**。
  - `Developer`: 绑定 `read_code_file`, `write_code_file`, `search_symbol_definition`。
  - `Reviewer`: 绑定 `run_unit_tests`。**严禁绑定读写文件工具**。

### B. 教学与理论落地级注释要求
本项目的代码实现具有强烈的教学和学习向性质，所有新增或修改的代码在注释中必须包含：
1. **教学级架构与设计注释**: 拒绝基础 Python 语法解释，把注释空间全部留给 Agent 架构、状态流转和机制。
2. **理论与实践关联**: 在代码的关键地方标注理论概念（如：“*此处在底层相当于 Hello-Agents 课程里讲过的 ToolResponse 协议/上下文持久化，只是在 LangGraph 中我们通过 Graph State 和 Checkpointer 这样来实现...*”）。
3. **决策与设计决策记录**: 每次设计复杂的 Agent 决策链路、条件边（Conditional Edge）或提示词工程（Prompt Engineering）时，在注释中写明：
   - **为什么要这样设计？** (设计初衷)
   - **有没有备选方案？** (Trade-offs 权衡)
   - **针对模型表现如何进行微调/做防御性编程？** (如防报错、防幻觉、处理原生 Gemini 异构工具格式等)

---

## 7. Git 提交规范
- 必须在完成**每一个阶段的功能**后立即进行 Git 提交。
- Git Commit Message 必须简洁明了，使用**中文**，去除明显的“AI 味”，保持普通程序员真实开发时的书写风格。
  - *示例*: `feat: 实现基于 FastAPI 的 WebSocket HITL 双向审批机制`
  - *示例*: `fix: 修复 Reviewer 节点因未检测到 REFACTOR_FAIL 产生无限循环的 Bug`
