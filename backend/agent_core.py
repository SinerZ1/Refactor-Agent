# ============================================================
# 教学说明: 后端 Agent 核心入口兼容层 (agent_core.py)
# ------------------------------------------------------------
# 为了保持系统高可维护性与高内聚，我们已经将大文件 `agent_core.py` 
# 进行微服务式的模块化拆分。
#
# 拆分后的子模块结构：
# - `agent/state.py`    : 图状态数据定义 (Graph State)
# - `agent/prompts.py`  : 角色提示词工程 (System Prompts)
# - `agent/tools.py`    : 多智能体基础工具库 (Tools)
# - `agent/nodes.py`    : 状态机执行节点逻辑 (Nodes)
# - `agent/edges.py`    : 条件路由流转控制 (Conditional Edges)
# - `agent/workflow.py` : 状态图编排与持久化编译 (workflow.compile)
#
# 本文件作为平滑重构的兼容包装器，将统一对外暴露 `app_graph`、`simple_refactor`
# 和 `stream_refactor`，确保应用启动及 REST/WebSocket 路由服务不受影响。
# ============================================================

from agent import app_graph, simple_refactor, stream_refactor

# 显式重导，方便第三方模块或外部测试
__all__ = ["app_graph", "simple_refactor", "stream_refactor"]
