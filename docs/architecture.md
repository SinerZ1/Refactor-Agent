# Refactor-Agent 架构说明

## 系统边界

Refactor-Agent 使用 FastAPI 统一承载会话、模型连接、SSE、WebSocket 和拓扑查询。
LangGraph 保持 `Architect → Developer → Reviewer` 的角色主链，并通过工具权限限制
各角色能力：

| 角色 | 工具权限 |
| --- | --- |
| Architect | `read_code_file`、`search_symbol_definition`、`query_neo4j_topology` |
| Developer | `read_code_file`、`write_code_file`、`search_symbol_definition` |
| Reviewer | `run_unit_tests` |

```mermaid
flowchart LR
    User["用户"]

    subgraph Frontend["Vue 3 前端 / Pywebview"]
        UI["代码工作台与 A2A 聊天室"]
        Diff["HITL 双栏 Diff 审批"]
        Dag["Vue Flow 计划 DAG"]
        Topology["ECharts 代码拓扑"]
    end

    subgraph Backend["FastAPI 后端"]
        Http["HTTP API"]
        Sse["POST 流式响应<br/>text/event-stream"]
        Ws["WebSocket<br/>/ws/refactor/{thread_id}"]
        Session["进程内会话与短期凭据"]

        subgraph Graph["LangGraph 状态机"]
            Architect["Architect"]
            Developer["Developer"]
            Interrupt["write_code_file<br/>HITL interrupt"]
            Reviewer["Reviewer"]
            Architect --> Developer --> Reviewer
            Developer --> Interrupt
            Interrupt -. "批准后 resume" .-> Developer
            Reviewer -. "失败时最多重试 3 次" .-> Developer
        end
    end

    subgraph State["状态与索引"]
        Redis["Redis Checkpointer"]
        Memory["MemorySaver 降级"]
        Ast["静态 AST 符号/调用图"]
        Neo4j["Neo4j 持久化调用图"]
    end

    User --> UI
    UI -->|"POST /api/refactor/stream"| Http
    Http --> Sse
    Sse -. "Agent 事件、工具日志、审批备用事件" .-> UI
    UI -->|"审批答复"| Ws
    Ws -->|"A2A 消息与审批请求"| UI
    Http --> Session
    Ws --> Session
    Http --> Graph
    Interrupt -->|"挂起 Graph State"| Redis
    Interrupt -. "Redis 不可用" .-> Memory
    Interrupt --> Diff
    Sse --> Dag
    Ast --> Architect
    Ast --> Neo4j
    Neo4j --> Topology
    Ast -. "Neo4j 不可用时直接供图" .-> Topology
```

这里的 SSE 由 `fetch()` 发起 POST 后消费流式响应，而不是浏览器 `EventSource`。
它负责单向、可恢复地传输版本化 Agent 事件。WebSocket 负责低延迟双向消息；
审批请求也会同时经 SSE 发出，因此 WebSocket 暂时不可用时仍能显示 HITL 弹窗，
审批答复则可通过 WebSocket 或 HTTP 备用接口提交。

## HITL 与状态恢复

```mermaid
sequenceDiagram
    actor User as 用户
    participant UI as Vue 前端
    participant API as FastAPI
    participant LG as LangGraph
    participant CP as Redis / MemorySaver

    User->>UI: 提交待重构代码
    UI->>API: POST /api/refactor/stream
    API->>LG: 启动 Architect → Developer
    LG->>CP: 保存 checkpoint
    LG-->>API: write_code_file interrupt
    API-->>UI: SSE approval.waiting
    API-->>UI: WebSocket approval_request（尽力发送）
    UI-->>User: 展示原代码与候选代码 Diff
    User->>UI: 批准或拒绝
    UI->>API: WebSocket / HTTP approval
    UI->>API: 重新发起 SSE 请求
    API->>CP: 读取挂起状态
    API->>LG: Command resume
    LG->>LG: Developer 继续，Reviewer 运行测试
    LG-->>UI: SSE review / run 终态事件
```

Redis 是首选 LangGraph checkpointer。未设置 `REDIS_URL` 或 Redis 连接失败时，工作流
自动使用 `MemorySaver`，不会阻止本地运行；代价是进程退出后 checkpoint 消失。

## 代码索引与 Neo4j 降级

后端启动时扫描 `CodeSmells/`，生成 AST 符号索引和直接调用关系。若 Neo4j 可用，
索引会写入项目隔离的图数据；若连接或写入失败，拓扑 API 和 Architect 查询工具改用
内存 AST 调用图。降级模式保留基本节点和调用边，但不提供数据库持久化能力。

## 计划 DAG 的当前语义

```mermaid
flowchart LR
    Plan["Architect 输出 refactor_plan"] --> Validate["后端校验任务 ID、路径、依赖与环"]
    Validate --> Event["SSE plan.created"]
    Event --> Render["Vue Flow 渲染计划节点与依赖边"]
    Runtime["工具与审查事件"] --> Reduce["前端事件归约器"]
    Reduce --> Render

    Render -. "仅展示计划和状态" .-> Boundary["当前不驱动 LangGraph 调度"]
    Boundary --> Actual["实际主流程仍是<br/>Architect → Developer → Reviewer"]
```

动态 DAG 当前是**计划可视化**：后端校验 Architect 输出的任务结构，前端据此布局，
并把文件工具、审批和审查事件映射到节点状态。当前版本尚未为每个任务创建独立执行
单元，也不会根据依赖边进行逐任务排队、并行或失败传播；真正的调度仍由现有 LangGraph
角色主链完成。
