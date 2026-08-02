# Windows 开发指南

本文面向全新 Windows 10/11 环境。所有示例使用 PowerShell 5.1 兼容语法，不要求 Redis、Neo4j 或真实模型凭据。

## 1. 前置条件

| 工具 | 版本 | 检查命令 |
| --- | --- | --- |
| Git | 当前受支持版本 | `git --version` |
| Python | 3.12.x | `py -3.12 --version` |
| Node.js | Node 24.12+ | `node --version` |
| npm | 随 Node 24 提供 | `npm.cmd --version` |

可选：

- Redis：用于跨进程 checkpoint 持久化；
- Neo4j：用于持久化调用图；
- Chromium：Playwright 命令会自动安装项目所需版本。

## 2. 首次安装

克隆并进入仓库：

```powershell
git clone <repository-url> Refactor-Agent
cd Refactor-Agent
```

### 后端

创建虚拟环境是唯一使用系统 Python 的引导步骤：

```powershell
cd backend
py -3.12 -m venv venv
venv\Scripts\python.exe -m pip install -r requirements-dev.txt
Copy-Item .env.example .env
```

`requirements-dev.txt` 通过 `requirements.lock` 约束已验证依赖，并以 editable 模式安装 `backend/pyproject.toml` 中的生产和开发依赖。安装后 `venv\Scripts\` 中会生成仓库统一脚本入口。

不要把 `.env`、ADC JSON、API Key、数据库口令或本机凭据路径加入 Git。默认复制出的 `.env.example` 可在不配置外部服务时启动应用。

### 前端

```powershell
cd ..\frontend
npm.cmd ci
```

`npm.cmd ci` 严格依据 `package-lock.json` 安装，不修改锁文件。

## 3. 启动 Web 开发环境

后端 PowerShell：

```powershell
cd Refactor-Agent\backend
venv\Scripts\python.exe app.py
```

默认监听 `http://127.0.0.1:8000`。

前端 PowerShell：

```powershell
cd Refactor-Agent\frontend
npm.cmd run dev
```

访问 `http://127.0.0.1:5173`。开发服务器默认连接本机 8000 端口；需要改地址时使用本地运行环境变量 `VITE_API_BASE_URL`，不要把机器专用配置提交到仓库。

## 4. 启动桌面应用

Pywebview 入口托管已构建的 Vue 静态资源：

```powershell
cd Refactor-Agent\frontend
npm.cmd run build

cd ..\backend
venv\Scripts\python.exe main_gui.py
```

如果 `frontend/dist/` 不存在，应先构建，而不是修改后端去依赖 Vite 开发服务器。

## 5. 后端验证

所有命令都在 `backend/` 中运行。

### 最小相关测试

```powershell
venv\Scripts\python.exe -m pytest tests\test_task_scheduler.py
```

把测试文件替换为当前改动的直接测试。隔离工作区、安全边界和传输契约的常用最小集：

```powershell
venv\Scripts\python.exe -m pytest `
  tests\test_isolated_workspace.py `
  tests\test_security_boundaries.py `
  tests\test_app_transport.py
```

### 完整只读门禁

```powershell
venv\Scripts\check-format.exe
venv\Scripts\check-sort-imports.exe
venv\Scripts\check-mypy.exe
venv\Scripts\check-lint.exe
venv\Scripts\test-coverage.exe
venv\Scripts\python.exe -m evals.run --mode offline
```

对应关系：

| 命令 | 检查 |
| --- | --- |
| `check-format.exe` | Black 格式 |
| `check-sort-imports.exe` | isort 导入顺序 |
| `check-mypy.exe` | 可部署后端与 Eval 类型 |
| `check-lint.exe` | Ruff |
| `test-coverage.exe` | 后端单元测试和只读 `backend/behavior_tests` 行为契约的 Pytest + coverage，最低 75% |
| `evals.run --mode offline` | 24 个 Agent 控制面场景与基准漂移 |

需要主动修复格式时才运行：

```powershell
venv\Scripts\format.exe
venv\Scripts\sort-imports.exe
```

这两个命令会改写文件。运行前后都应检查 `git diff`，避免把无关格式变化混入提交。

## 6. 前端验证

所有命令都在 `frontend/` 中运行：

```powershell
npm.cmd run type-check
npm.cmd run test:unit -- --run
npm.cmd run build
npx.cmd playwright install chromium
npm.cmd run test:e2e -- --project=chromium
```

`test:unit` 默认是 Vitest 入口；本地自动化时加 `-- --run` 确保单次退出。Playwright E2E 会启动独立 Vite 服务器，并在测试内 mock 后端接口，因此不需要模型、Redis 或 Neo4j。

仓库的 `npm.cmd run lint` 和 `npm.cmd run format` 会改写文件，只在明确准备修复相关文件时执行，并在之后检查 diff。

## 7. 可选基础设施

### Redis

在本地 `.env` 设置 `REDIS_URL` 后，工作流优先使用 `RedisSaver`。未配置、连接失败或 setup 失败时自动使用 `MemorySaver`，健康检查显示 `degraded`。

不要在命令、文档或日志中粘贴带口令的真实 Redis URL。验证降级不需要运行 Redis：

```powershell
venv\Scripts\python.exe -m pytest `
  tests\test_app_transport.py::test_get_checkpointer_no_redis_url
```

### Neo4j

只有同时提供 `NEO4J_URI`、`NEO4J_USER`、`NEO4J_PASSWORD` 才会尝试建立 driver。缺少凭据或连接失败时保留内存 AST 调用图：

```powershell
venv\Scripts\python.exe -m pytest `
  tests\test_backend_contracts.py::test_neo4j_missing_credentials_skips_driver_and_uses_ast
```

不得为了测试成功删除 AST 降级路径。

## 8. 模型配置

应用支持 OpenAI 兼容 API、Gemini Studio 和 Google Vertex AI。模型 Key 可以：

- 在页面中为当前运行临时输入；
- 通过本地 `backend/.env` 提供；
- Vertex ADC 使用本机默认凭据发现。

页面不会把 API Key 写入 localStorage；后端不会把 Key 或 session token 写入 checkpoint。真实模型连接不是本地单元测试和 CI 的前置条件。

## 9. CI 对照

`.github/workflows/ci.yml` 包含两个 Windows 作业：

### Backend · Windows · Python 3.12

1. 创建 `backend/venv/`；
2. 按锁文件安装依赖；
3. 在空外部服务配置下验证 Redis/Neo4j 降级；
4. 运行 Black、isort、mypy、Ruff；
5. 运行全量单元测试与覆盖率；
6. 运行离线 Agent Eval。

### Frontend · Windows · Node 24

1. `npm.cmd ci`；
2. 类型检查；
3. Vitest 单元测试；
4. 生产构建；
5. 安装 Chromium；
6. 只运行 Chromium Playwright 项目；
7. 无论成功或失败均上传可用的 Playwright HTML 报告。

CI 不读取仓库 secrets，也不启动 Redis、Neo4j 或模型服务。

## 10. 提交前清单

```powershell
git status --short
git diff -- <目标文件>
```

确认：

- 只包含当前主题；
- 没有 `.env`、报告输出、凭据文件、真实密钥或本机绝对路径；
- 最小相关测试先通过；
- 风险对应的完整检查已通过；
- 没有删除 Redis、Neo4j 或模型服务的降级；
- `Architect → Developer → Reviewer` 与角色工具权限未改变。

提交使用中文 Conventional Commit，例如：

```text
docs: 完善项目展示与工程决策文档
```

## 11. 常见问题

### `venv\Scripts\*.exe` 不存在

确认在 `backend/` 创建虚拟环境并安装了 `requirements-dev.txt`。不要改用系统全局的 Black、Ruff 或 Pytest，否则可能绕过锁定版本和脚本工作目录。

### 健康检查显示 Redis/Neo4j `degraded`

这是可选组件未配置时的预期状态。服务总状态仍应为 `ok`。只有需要持久化 checkpoint 或图数据时才配置组件。

### Playwright 找不到浏览器

在 `frontend/` 运行：

```powershell
npx.cmd playwright install chromium
```

### E2E 端口被占用

默认 E2E 端口是 4180。可为当前 PowerShell 临时选择其他端口：

```powershell
$env:E2E_PORT = "4181"
npm.cmd run test:e2e -- --project=chromium
Remove-Item Env:E2E_PORT
```

### 后端测试产生 `.refactor-workspaces/`

正常结束会自动清理。若进程被强制终止，先确认没有运行中的重构任务，再人工检查该目录；不要把它提交到 Git。
