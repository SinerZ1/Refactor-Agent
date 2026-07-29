# ADR-0002：Redis Checkpointer 与 MemorySaver 降级

- 状态：已采纳
- 日期：2026-07-29

## 背景

LangGraph 的 HITL 依赖 checkpoint 在 `interrupt` 与 `resume` 之间保存状态。Redis 适合提供进程外持久化，但把 Redis 设为本地开发、CI 和演示的硬依赖，会显著提高首次验证成本并让基础设施故障阻断安全功能测试。

## 决策

- 配置 `REDIS_URL` 时，先验证连接并初始化 `RedisSaver`。
- 未配置、连接失败或 setup 失败时，自动使用进程内 `MemorySaver`。
- 降级不移除 LangGraph interrupt、审批、证据门禁或隔离工作区。
- 健康检查单独报告 Redis 的 `ok/degraded`、后端类型和脱敏原因。
- 服务总健康在控制面仍可工作时保持 `ok`。
- CI 不启动 Redis，并明确测试 `MemorySaver` 路径。

## 原因

Checkpointer 是可替换的状态存储适配器，HITL 是上层状态机协议。把二者解耦后，本地和 CI 可以验证完整控制语义，生产或长会话环境再选择 Redis 的持久化能力。

## 后果

正面：

- 全新 Windows 环境无需 Docker 即可运行；
- Redis 故障不会把候选变更直接应用到源码；
- 降级行为可在单元测试和健康检查中观察；
- CI 完全离线且确定。

代价：

- MemorySaver 中的 checkpoint 在进程退出后丢失；
- 进程重启后的待审批 run 无法恢复；
- 多进程部署不能共享 MemorySaver；
- 运维必须关注 `degraded`，不能只看 HTTP 200。

## 被否决方案

- Redis 强依赖：提高演示和贡献门槛，基础设施故障掩盖控制面测试。
- 自研文件 checkpoint：需要承担锁、迁移、敏感字段和崩溃一致性。
- 降级时关闭 HITL：违反“故障不得绕过安全门禁”的不变量。
