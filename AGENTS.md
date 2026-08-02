# Codex Agent 指南 - Refactor-Agent

本文件适用于整个仓库。若子目录以后出现更具体的 `AGENTS.md`，以离目标文件最近的规则为准。

## 1. 软件/硬件系统环境 (System Environment)
- **操作系统**: Windows 10/11 (`win32`)
- **执行 Shell**: PowerShell 5.1 (在执行终端命令时注意 Windows 路径分隔符 `\` 与命令行差异)
- **Python 环境**: Python `3.12.10`，使用虚拟环境 `backend/venv/`
- **NodeJS 环境**: Node.js `v24.17.0`，使用 `npm` 进行包管理
- **Docker 容器**: Docker version `29.5.3`。
  - 在 Docker 中部署了 **Redis server v=8.8.0**，容器名称为 `my-redis`
  - 命令行入口: `docker exec -it my-redis redis-cli`
- **Neo4j 数据库**: Neo4j Desktop 2。
  - 本地实例名称: `Cyber-Refactor-Agent-Database` (Version: 2026.05.0)
  - 连接 URI: `neo4j://127.0.0.1:7687` (或 `bolt://127.0.0.1:7687`)
  - 认证信息必须通过 `backend/.env` 中的 `NEO4J_USER` 与 `NEO4J_PASSWORD` 提供；禁止在受版本控制的文件中记录明文口令。

---

## 2. 核心目录与模块化架构
- `CodeSmells/`: 存放待重构的 Python 历史代码（包含故意设计的坏味道代码）。
- `backend/`: FastAPI 后端与 LangGraph 多智能体引擎。
  - **核心重构变更**: `agent_core.py` 现已只是无缝重导出的包装层。真正的核心逻辑被完全拆分在 `backend/agent/` 子包内：
    - `state.py`: 图状态定义。
    - `prompts.py`: 角色 System Prompts。
    - `tools.py`: 工具库。
    - `nodes.py`: 节点逻辑（支持运行时隔离实例化大模型）。
    - `edges.py`: 条件路由（审查重试与结束）。
    - `workflow.py`: 状态图编排编译。
    - `scheduler.py`: Architect 任务 DAG 校验、确定性拓扑调度、任务重试与依赖阻断。
    - `workspace.py`: run 级双快照隔离、聚合 diff、冲突检测、原子应用与补偿回滚。
  - `backend/evals/`: 离线 fake model 与可选真实模型 Agent Eval；报告不得保存 Prompt、原始模型响应或凭据。
  - `backend/scripts/`: 重建的核心脚本包，使用 `.pth` 注入虚拟环境解决 Windows 下子进程命令报错。
- `frontend/`: Vue 3 + Vite + TypeScript 前端；会话、SSE、WebSocket、审批、Agent 聊天与工作区视图由 composable/组件分层管理。
- `docs/`: 当前执行架构、安全模型、Agent Eval、开发指南和 ADR。

---

## 3. 后端开发环境与快捷命令
所有后端命令必须在 `backend/` 目录下执行，且必须使用虚拟环境（venv）路径：
- **Python 解释器**: `venv/Scripts/python.exe`
- **启动服务**: `venv/Scripts/python.exe app.py` (运行在 `http://127.0.0.1:8000`)
- **启动 GUI 桌面应用程序**: `venv/Scripts/python.exe main_gui.py` (基于 Pywebview，内置托管 Vue 3 静态编译前端)
- **虚拟环境内置脚本** (位于 `backend/venv/Scripts/`，在 Windows 下直接运行)：
  - 代码格式化/导入排序: `venv/Scripts/format.exe`, `venv/Scripts/sort-imports.exe`
  - 格式与类型检查: `venv/Scripts/check-format.exe`, `venv/Scripts/check-sort-imports.exe`, `venv/Scripts/check-mypy.exe`, `venv/Scripts/check-lint.exe`
  - 运行单元测试: `venv/Scripts/test.exe`
  - 扫描死代码: `venv/Scripts/find-dead-code.exe`

不要调用系统 Python，也不要在仓库根目录直接运行这些后端脚本；部分脚本依赖 `backend/` 作为当前工作目录。

---

## 4. 数据库与 RAG 索引降级机制
- **静态 AST 解析**: 后端在启动时会自动扫描 `CodeSmells/`，生成 AST 符号索引（`SYMBOL_INDEX`）。
- **Neo4j 数据库**: 仅在凭据完整时尝试连接并写入依赖关系；凭据缺失或服务不可用时，系统自动优雅降级为**内存 AST 调用图模式**，不会导致崩溃。
- **Redis 记忆持久化**: 优先读取 Docker 容器中的 Redis (`REDIS_URL`) 进行状态持久化，否则优雅降级为 `MemorySaver`。
- **健康检查语义**: Redis 与 Neo4j 的降级状态必须分别报告；基础设施降级不等于服务不健康，也不得绕过 LangGraph interrupt、证据门禁或隔离工作区。

---

## 5. 前端开发环境与命令
所有前端命令必须在 `frontend/` 目录下运行，使用 `npm` 进行包管理：
- **启动开发服务器**: `npm run dev` (默认端口 `5173`)
- **类型检查**: `npm run type-check` (基于 `vue-tsc --build`)
- **前端 Lint**: `npm run lint` (并行运行 `oxlint . --fix` 与 `eslint . --fix --cache`)
- **前端格式化**: `npm run format` (基于 `prettier --write`)
- **单元测试**: `npm run test:unit`
- **E2E 测试**: `npm run test:e2e`

---

## 6. 前后端分离、多通道架构与可视化面板
本项目包含极强的协同架构与可视化呈现：
- **双轨通信通道**:
  - **流式 SSE (单向流)**: 把 Agent 的 Thought（思考过程）、Tool Call（终端执行日志）和权威 `plan.*`、`task.*` 事件实时推送到 Vue 3 前端；必须保留心跳、禁缓存/代理缓冲和断连资源释放。
  - **WebSocket (全双工)**: 承载 DeveloperAgent、ReviewerAgent 的 A2A 聊天和 HITL 恢复。Developer 只修改隔离工作区；所有任务通过 Reviewer 后才对完整聚合 diff 触发一次审批。
- **动态多模型适配器**: 支持 OpenAI, DeepSeek, Zhipu, Gemini Studio, Google Vertex AI。支持本地 ADC (Application Default Credentials) 凭证文件加载。
- **双图并进视图**:
  - **图谱探查器 (ECharts)**: 基于 Neo4j 的代码依赖关系力导向网状图谱。
  - **任务 DAG 追踪 (Vue Flow)**: 仅依据后端 `plan.*`、`task.*` 权威事件和状态快照渲染任务、依赖、重试与阻断状态，不得从自然语言日志猜测 DAG 状态。
- **Markdown 安全边界**: 所有进入 `v-html` 的 Agent、SSE 与 WebSocket Markdown 必须经过统一 DOMPurify 清洗，同时保留表格和代码块能力。

---

## 7. LangGraph 多智能体协同约束与代码实现要求
在修改 `backend/agent/` 内逻辑时，必须严格遵循：
- **工作流节点**: `Architect` -> `Developer` -> `Reviewer`。
- **动态 DAG 控制面**: Architect 输出必须先经过结构化校验；调度器采用稳定、可复现的串行拓扑顺序，管理任务级 `pending/running/completed/failed/blocked` 状态、最多 3 次重试、依赖阻断和预算熔断。不得用自然语言日志替代权威状态。
- **审查条件路由**:
  - 失败时，回复内容 **必须** 包含 `【REFACTOR_FAIL】` 和唯一结构化失败对象；Reviewer 可指定 `failed_task_ids`，由调度器重开指定任务及其传递下游。
  - 成功时，回复内容 **必须** 包含 `【REFACTOR_SUCCESS】`，且结构化证据门禁通过后才可进入最终审批；成功文本本身不是充分条件。
  - Reviewer 成功证据必须与当前变更摘要绑定：存在成功写入、最新变更已运行包含 `CodeSmells` 的白名单测试、退出码为 0、输出包含成功标记，且任务/计划状态一致。Developer 再次写入后旧证据立即失效。
- **工具权限**:
  - `Architect`: 绑 `read_code_file`, `search_symbol_definition`, `query_neo4j_topology`。严禁写文件。
  - `Developer`: 绑 `read_code_file`, `write_code_file`, `search_symbol_definition`；动态 DAG 模式下只能写当前任务声明的文件，且所有读写均指向 run 级隔离工作区。
  - `Reviewer`: 绑 `run_unit_tests`。严禁读写文件。
- **隔离与提交协议**:
  - 每次 run 建立 `baseline/working` 双快照；Developer 和 Reviewer 全程只操作 `working`，任何非成功终态均不得修改真实 `CodeSmells/`。
  - Reviewer 通过后固化变更文件、完整统一 diff 与 baseline/working 哈希，并只触发一次最终聚合 HITL。
  - 批准时必须先检查真实源码基线冲突和审批后工作区篡改，再通过同目录临时文件、原子替换与补偿回滚提交多文件变更；拒绝、断连、预算失败或应用失败必须保持真实源码不变并清理隔离目录。
- **网络与日志安全**: 所有出站 URL 必须校验结构、DNS 解析结果、私网/回环/链路本地地址和云元数据地址；Redis、Neo4j 与模型供应商错误必须脱敏，禁止记录凭据、Prompt 或原始模型响应。

### 教学与理论落地级注释要求
本项目的代码必须具有强烈的教学性质：
1. **教学级架构与设计注释**: 拒绝基础 Python 语法解释，把注释留给 Agent 架构、状态流转机制。
2. **理论与实践关联**: 在代码中关联学术概念（如：“*此处相当于 Hello-Agents 讲过的 ToolResponse 协议，在 LangGraph 中通过 Graph State 与 Checkpointer 实现...*”；HITL 相当于中断与状态机覆写）。
3. **决策记录**: 写明复杂决策链路的初衷、Trade-offs 权衡、防御性编程与防幻觉设计。

---

## 8. 项目阶段状态（阶段 0–8 已全部完成）
- **阶段 0—开发基线与说明骨架**: 根 README 已覆盖前后端启动、基础设施降级路径和动态 DAG 能力边界。
- **阶段 1—Markdown 注入修复**: 建立统一 DOMPurify 安全渲染边界，覆盖 Agent 与 WebSocket 消息并保留 Markdown、表格和代码块。
- **阶段 2—Reviewer 结构化证据硬门禁**: 测试记录绑定当前变更摘要；写入、测试套件、退出码、成功标记和证据时效共同决定成功终态。
- **阶段 3—后端安全与接口契约**: 统一出站网络校验和错误脱敏；Neo4j/Redis 安全降级；移除不支持 HITL 的同步接口；SSE 支持心跳、禁缓存/缓冲和断连清理。
- **阶段 4—前端巨型组件拆分**: 会话、流式通信、审批、Agent 聊天和工作区视图已解耦为可测试 composable/组件；另行修复依赖图谱主题硬编码。
- **阶段 5—真实动态任务 DAG 调度**: Architect 计划成为执行控制面，支持稳定串行拓扑、任务状态/重试、依赖阻断、预算失败、HITL 原任务恢复和 Reviewer 结构化打回。
- **阶段 6—隔离工作区与原子应用**: 建立 run 级双快照、结构化哈希、完整聚合 diff、一次最终 HITL、基线冲突检测、原子替换和补偿回滚。
- **阶段 7—Agent Eval 与可观测性**: 覆盖代码质量、工作流失败、安全攻击、预算熔断和 HITL 拒绝；支持离线 fake model 基准回归和受安全边界约束的可选真实模型评测。
- **阶段 8—CI、文档与求职展示**: 建立完全离线可验证的 Windows CI，补齐真实执行架构、安全模型、Agent Eval、开发指南和五项核心 ADR；README 可供新用户和面试官快速复现核心能力。

后续修改必须把上述能力视为当前基线；若替换已采纳的架构决策，应新增 ADR 并把旧 ADR 标记为“已取代”，不得直接改写历史决策。

---

## 9. Git 提交规范
- 完成**每一个阶段的功能**后立即进行 Git 提交。
- Commit Message 沿用仓库现有的 Conventional Commits 风格：`<type>: <中文摘要>`。冒号使用半角字符，冒号后保留一个空格，标题末尾不加句号。
- `type` 应与改动性质一致：新功能使用 `feat`，缺陷修复使用 `fix`，文档使用 `docs`，代码重构使用 `refactor`，工程维护使用 `chore`；不要为了强调改动而随意组合类型。
- 中文摘要应简洁说明实际结果，优先使用“实现”“支持”“修复”“解决”“重构”“更新”“删除”等自然动词，避免空泛表述、营销措辞和“AI 味”。例如：`feat: 支持前端动态配置模型`、`fix: 修复测试工具的执行路径问题`、`docs: 更新 Git 提交规范`。
- 每个提交只包含一个完整主题；功能与其直接相关的测试可放在同一提交，无关修复或格式化应拆分提交。
- 提交前先检查 `git status --short` 和目标文件的 diff，只暂存本阶段修改；不得覆盖、回滚或顺带提交用户已有改动。

---

## 10. 实施与验证流程
- 修改前先阅读目标模块及其直接调用方，优先复用现有抽象，避免无关的大范围重写。
- 使用 PowerShell 5.1 兼容语法；搜索文件和文本时优先使用 `rg --files` 与 `rg`。
- 验证遵循“最小相关集优先”：后端改动先运行对应检查或测试，再按风险扩大到完整检查；前端改动至少运行 `npm run type-check`，涉及行为时补充相关单元测试。
- `npm run lint`、`npm run format` 以及后端格式化脚本会改写文件，执行前确认范围，执行后检查 diff，避免混入无关格式变化。
- Neo4j、Redis 或外部模型服务未启动时，应验证既有降级路径，不得为了让测试通过而移除降级机制。
- 修改配置、日志或示例时不得新增密钥、令牌或本机凭据；已有敏感配置也不得复制到新文件或输出到日志。
- CI 和 Agent Eval 默认必须完全离线、可复现；真实模型评测只能作为显式启用的可选路径。
