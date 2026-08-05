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
- `CodeSmells/`: 存放待重构的 Python 历史代码（包含故意设计的坏味道代码，作为教学与重构示例）。
- `backend/`: FastAPI 后端与 LangGraph 多智能体引擎。
  - `agent/`: 核心重构 agent 包：
    - `state.py`: 图状态定义、记录合并器、完整工作区快照摘要与证据检查。
    - `prompts.py`: 角色 System Prompts。
    - `tools.py`: 白名单工具库（只读文件、隔离写入、测试套件、符号查询）。
    - `nodes.py`: 角色节点与预算用量记账。
    - `edges.py`: 条件路由（审查重试与审查协议校验）。
    - `workflow.py`: 生产状态图装配与 Checkpointer 连接。
    - `scheduler.py`: Architect 任务 DAG 校验、确定性拓扑调度、任务重试、下游重开与依赖阻断。
    - `workspace.py`: run 级双快照隔离、聚合 diff、冲突检测、乐观并发 apply 与安全补偿回滚。
    - `path_policy.py`: 路径规范化、仓库/重构根判定与 `backend/behavior_tests` 可信只读策略。
    - `run_lifecycle.py`: 逻辑 run 的统一资源所有者，管理 producer 线程有界 join、协作式停止信号、临时凭据撤销、checkpoint 删除与工作区回收。
    - `run_status.py`: 容量受控 (容量 512)、TTL 受控 (30 分钟) 的脱敏 run 生命周期与 apply 失败状态快照注册表，支持恢复读取。
  - `behavior_tests/`: 移出 Agent 可写区（`CodeSmells/`）的可信行为契约套件，Reviewer 通过 `REFACTOR_CODE_ROOT` 对当前 run 的隔离 `working/CodeSmells` 执行验证。
  - `evals/`: 离线 scripted model 与可选真实模型 Agent Eval：
    - `control_plane.py`: 驱动真实生产 `build_agent_graph` 的控制面场景运行器。
    - `models.py`: `ScriptedFakeModel` 确定性脚本模型与 `LiveEvaluationModel` 只读判定器。
    - `runner.py`: 自动化行为断言评分与基准漂移比较 (`baseline_drift`)。
    - `scenarios.py`: 21 个覆盖代码质量、跨文件 DAG、攻击防护、证据门禁、预算熔断、HITL 与事务冲突的离线场景。
  - `scripts/`: 核心工程脚本包，在虚拟环境中通过 `python -m scripts` 执行。
- `frontend/`: Vue 3 + Vite + TypeScript 前端；会话、SSE、WebSocket、审批、Agent 聊天与工作区视图由 composable/组件分层管理。
- `docs/`: 当前执行架构、安全模型、Agent Eval、开发指南和 ADR 索引。

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
  - 运行离线评测: `venv/Scripts/python.exe -m evals.run --mode offline`

*注意（Windows pytest 临时目录约束）*: 在 Windows 环境下，运行 `pytest` 时如遇到系统临时目录权限限制 (`PermissionError: WinError 5`)，通过 `--basetemp`（如 `--basetemp=.cache/pytest_tmp`）即可干净通过；内置脚本 `test-coverage.exe` 已包含完整覆盖率和测试支持。

不要调用系统 Python，也不要在仓库根目录直接运行这些后端脚本。

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
- **单元测试**: `npm run test:unit -- --run`
- **E2E 测试**: `npm run test:e2e -- --project=chromium`

---

## 6. 前后端分离、多通道架构与可视化面板
- **双轨通信通道**:
  - **流式 SSE (单向流)**: 把 Agent 的 Thought、Tool Call 和权威 `plan.*`、`task.*`、`run.lifecycle.updated` 事件实时推送到 Vue 3 前端；具备 SSE 心跳、禁缓存/代理缓冲和断连资源释放。
  - **WebSocket (全双工)**: 承载 A2A 聊天和 HITL 低延迟通知/恢复。Developer 只修改隔离工作区；所有任务通过 Reviewer 后才对完整聚合 diff 触发一次审批。
- **动态多模型适配器**: 支持 OpenAI, DeepSeek, Zhipu, Gemini Studio, Google Vertex AI。支持本地 ADC (Application Default Credentials) 凭证文件加载与安全脱敏展示。
- **双图并进视图**:
  - **图谱探查器 (ECharts)**: 基于 Neo4j/AST 的代码依赖关系力导向网状图谱。
  - **任务 DAG 追踪 (Vue Flow)**: 仅依据后端 `plan.*`、`task.*` 权威事件和状态快照渲染任务、依赖、重试与阻断状态，不得从自然语言日志猜测 DAG 状态。
- **Markdown 安全边界**: 所有进入 `v-html` 的 Agent、SSE 与 WebSocket Markdown 必须经过统一 DOMPurify 清洗，同时保留表格和代码块能力。

---

## 7. LangGraph 多智能体协同约束与代码实现要求

### 7.1 工作流与角色权限
- **工作流节点主链**: `Architect` -> `Developer` -> `Reviewer`。
- **工具权限划分**:
  - `Architect`: 绑定 `read_code_file`, `search_symbol_definition`, `query_neo4j_topology`。严禁写文件。
  - `Developer`: 绑定 `read_code_file`, `write_code_file`, `search_workspace_symbol_definition`；动态 DAG 模式下只能写当前任务声明的文件，读取与符号查询仅覆盖当前任务及传递依赖文件，且所有读写均指向 run 级隔离工作区 `working` 副本。
  - `Reviewer`: 绑定 `run_unit_tests`。严禁读写文件或搜索符号。

### 7.2 可信测试信任域约束
- `backend/behavior_tests/` 是 Reviewer 使用的可信行为契约，属于 Agent 不可写信任域。
- Architect 计划、Developer 写入、聚合 diff 和正式 apply 都由 `path_policy.py` 严格禁止触及 `backend/behavior_tests` 与 `codesmells/tests`。
- Reviewer 通过受控环境变量 `REFACTOR_CODE_ROOT` 测试当前 run 的 `working/CodeSmells` 隔离副本，不得测试原始用户源码或错误工作区。
- 禁止把可信测试复制回 `CodeSmells/` 或其他 Agent 可写目录作为伪造 oracle。

### 7.3 完整工作区证据约束
- 测试证据必须基于完整受管理工作区（`CodeSmells/` 包含的所有文件）的确定性快照摘要 (`workspace_snapshot_digest`)，绝不能只基于变更记录条数或 Agent 自报列表。
- `change_records` 仅用于 UI/审计展示。
- 测试过程产生源码副作用（`workspace_stable=False`）时测试记录标记为不通过。
- Developer 任何新的写入都会显式清空旧测试记录 (`test_run_records = []`)，使旧证据立即失效。
- Reviewer 成功、最终聚合审批和正式 apply 必须核对同一工作区内容身份 (`current_digest == final_digest == reviewed_digest`)。

### 7.4 工作区符号与依赖上下文
- Architect 检索用户原始源码静态索引 (`get_symbol_definition_content`)；
- Developer 检索当前 run 的 `working` 树私有快照 (`search_workspace_symbol_definition`)，能准确反映写入、删除、重命名与语法错误。
- 绝不得通过修改进程全局索引来实现 run 隔离；并发 run 不得共享可变候选索引。
- 动态任务只可读取当前任务文件及其传递依赖文件；下游依赖上下文通过有界、可机器解析的 `DEPENDENCY_RESULTS` JSON 传递。
- Reviewer 打回重开任务时，必须清除指定任务及其传递下游的旧结果。

### 7.5 Run 生命周期与资源所有权
- `RunLifecycle` 是 run 资源的统一所有者，负责管理 producer 线程有界 join、协作式停止信号 (`stop_requested`)、临时凭据撤销、checkpoint 删除与工作区回收。
- 清理操作必须幂等，并正确处理 cancellation 异常。
- 只有在 checkpoint 存在有效最终审批 interrupt、身份一致且快照摘要匹配时，才建立显式 `HitlRetention`；非 HITL 终态、拒绝或取消必须清理工作区与 checkpoint。
- producer 超时未退出时，由受跟踪的后台 task (`_finish_after_producer`) 在线程真正结束后延迟回收，不得提前删除仍被线程访问的资源。
- WebSocket 是会话级资源，不得在单个 run 结束时被错误关闭。

### 7.6 生命周期与事务状态可观测性
- `RunStatusRegistry` 是单进程、容量 512、TTL 30 分钟的脱敏运行状态快照注册表，支持 SSE 断连后的恢复读取。
- 快照查询必须校验 `thread_id` 与 `session_token`。
- `lifecycle_status` 明确区分 `running`、`waiting_for_hitl`、`cancelling`、`cleanup_pending`、`cleanup_completed`、`cleanup_failed`、`completed` 与 `failed`。
- apply 失败转换为 `public_workspace_apply_failure`，只包含冲突分类、`rollback_status` (`complete/partial/not_started`)、受影响的 `CodeSmells/` 相对路径和脱敏 `recovery_ids`（SHA-256 摘要）；绝不泄露本机绝对路径或恢复材料源码。

### 7.7 正式应用乐观并发控制与安全补偿边界
- 对同一规范化源码根目录的正式 apply 必须在进程内互斥锁 (`_apply_critical_section`) 中串行执行。
- 临界区完整覆盖证据复核、基线预检、同目录临时文件准备、逐文件替换、冲突检测、补偿回滚与临时文件清理。
- 提交前全量预检与每个文件 `os.replace` 前，必须重检真实源码与 baseline 一致 (`_assert_target_matches_baseline`)；组件包含符号链接或 reparse point 时失败关闭。
- 补偿回滚只有在目标仍等于本事务写入摘要时才进行恢复；若第三方已再次修改目标，自动回滚拒绝覆盖它，并返回 `rollback_status="partial"`，备份文件改名为同目录 `.refactor-recovery-<transaction_id>` 恢复材料留待人工核对。
- **边界**：进程内锁仅协调本服务实例，无法约束外部编辑器；多文件替换是应用级乐观并发控制与安全补偿，不是文件系统级或数据库级的强原子事务，不保证掉电恢复。

### 7.8 离线 Eval 约束与防循环验证
- `input`、`script`、`actual` 与 `expected` 必须严格分离。
- `ScriptedFakeModel` 构造函数只接受 `script`，无法访问 `scenario.expected`；`expected` 修改只改变断言判定，绝不改变 `actual` 采集。
- 离线 Eval 驱动生产 `build_agent_graph` 图工厂，真实执行节点、ToolNode、scheduler、条件边、证据门禁与工作区事务。
- `ScriptedFakeModel` 必须对角色、任务 ID、重试次数、工具轮次与图阶段进行严格校验；未声明调用、顺序错误或脚本未消费完均立即触发 `AssertionError` 失败（fail-closed）。
- 默认 `evals.run --mode offline` 完全离线，不读取 `.env`，使用临时源码根与 `MemorySaver`；默认只读比较基准 `baselines/offline.json`，存在漂移时返回非零退出码，绝不自动覆盖基准。只有显式 `--update-baseline` 参数才更新基准。
- 报告不得保存 Prompt、模型原始响应、源码全文、完整 diff、凭据或用户绝对路径。

### 7.9 前端状态约束与代际隔离
- 切换 session 或发起新重构时，统一调用 `resetRunScopeState` 清空 DAG、聊天、日志、审批、预算和生命周期状态。
- 主题与模型选择属于用户级配置，不参与重置。
- 所有异步数据写入（SSE、WebSocket、审批 HTTP、状态快照）必须同时校验 connection generation、`thread_id` / `session_token` 身份以及 `activeRunId`；旧会话或旧 run 的迟到消息必须被忽略。
- 所有传给 `v-html` 的 Markdown 必须经过 `renderSafeMarkdown()` 清洗。

---

## 8. 项目阶段状态与加固基线

### 阶段 0–8（基础能力全量完成）
- **阶段 0—开发基线与说明骨架**: README 覆盖启动、基础设施降级与动态 DAG 边界。
- **阶段 1—Markdown 注入修复**: DOMPurify 安全渲染边界，覆盖 Agent 与 WebSocket 消息。
- **阶段 2—Reviewer 结构化证据硬门禁**: 结构化证据门禁决定成功终态。
- **阶段 3—后端安全与接口契约**: 出站网络校验、错误脱敏、Redis/Neo4j 降级与 SSE 心跳/清理。
- **阶段 4—Frontend 前端组件解耦**: 模块化 composable/组件分层。
- **阶段 5—真实动态任务 DAG 调度**: 结构化计划控制面、拓扑调度、重试与阻断。
- **阶段 6—隔离工作区与原子应用**: run 级双快照、一次聚合 HITL、基线冲突检测与安全补偿。
- **阶段 7—Agent Eval 与可观测性**: 21 个离线控制面场景、指标体系与报告隐私边界。
- **阶段 8—CI、文档与求职展示**: Windows 离线 CI、五项核心 ADR 与完整工程文档。

### 阶段完成后的可信性加固基线
1. **可信执行链加固**: 可信行为契约迁至 Agent 不可写的 `backend/behavior_tests`；Reviewer 测试绑定完整工作区快照摘要；Developer 符号查询限制在授权任务及传递依赖范围内。
2. **生命周期与并发事务加固**: `RunLifecycle` 拥有资源生命周期，支持有界等待与延迟清理；`RunStatusRegistry` 提供脱敏快照恢复读取；正式 apply 实施进程内分根锁、乐观重检与安全补偿。
3. **真实控制面 Eval 与前端状态收尾**: 离线 Eval 驱动生产控制面图，实现 fail-closed 脚本协议与防循环验证；前端构建多代际（generation）与 `run_id` 双重隔离，抵御迟到事件污染。

后续修改必须将上述基线视为既有约束；若替换采纳的决策，应新增 ADR 并把旧 ADR 标记为“已取代”。

---

## 9. Git 提交规范
- 完成**每一个阶段或主题的独立功能/文档**后立即进行 Git 提交。
- Commit Message 沿用 Conventional Commits 风格：`<type>: <中文摘要>`。冒号使用半角字符，冒号后保留一个空格，标题末尾不加句号。
- `type` 选项：`feat`（新功能）、`fix`（修复）、`docs`（文档）、`refactor`（重构）、`chore`（工程维护）。
- 摘要使用自然动词，例如：`feat: 支持前端动态配置模型`、`fix: 修复测试工具的执行路径问题`、`docs: 更新可信执行与评测开发约束`。
- 每个提交只包含一个完整主题；提交前先检查 `git status --short` 和 diff，只暂存目标修改。

---

## 10. 实施与验证流程
- 修改前先阅读目标模块及其直接调用方，优先复用现有抽象。
- 使用 PowerShell 5.1 兼容语法；搜索文件和文本时优先使用 `rg --files` 与 `rg`。
- 后端改动必须在 `backend/` 目录下使用 `venv/Scripts/` 运行对应检查和测试；前端改动必须在 `frontend/` 目录下运行 `npm run type-check` 与 `npm run test:unit -- --run`。
- `npm run lint`、`npm run format` 以及后端格式化脚本会改写文件，执行前确认范围，执行后检查 diff。
- Neo4j、Redis 或外部模型服务未启动时，必须验证既有降级路径，不得删除降级逻辑。
- CI 和 Agent Eval 默认必须完全离线、可复现；真实模型评测只能作为显式启用的可选路径。
