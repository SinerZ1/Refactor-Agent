# 架构决策记录

ADR 记录已经落地且会影响后续演进的决策。状态为“已采纳”表示当前生产路径按该决策实现；若未来替换，应新增 ADR 并把旧记录标为“已取代”，而不是改写历史背景。

| ADR | 决策 | 状态 |
| --- | --- | --- |
| [0001](0001-dual-channel-communication.md) | SSE 与 WebSocket 双通道通信 | 已采纳 |
| [0002](0002-redis-checkpointer-memory-fallback.md) | Redis Checkpointer 与 MemorySaver 降级 | 已采纳 |
| [0003](0003-reviewer-least-privilege.md) | Reviewer 最小权限 | 已采纳 |
| [0004](0004-dynamic-dag-scheduling.md) | 动态 DAG 的确定性串行调度 | 已采纳 |
| [0005](0005-isolated-workspace-atomic-apply.md) | 隔离工作区与最终原子应用 | 已取代（提交部分） |
| [0006](0006-stream-run-lifecycle.md) | 流式 run 的显式生命周期与有界回收 | 已采纳 |
| [0007](0007-optimistic-concurrent-apply.md) | 正式应用的乐观并发控制与安全补偿 | 已采纳 |
