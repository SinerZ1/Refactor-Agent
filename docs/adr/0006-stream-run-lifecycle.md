# ADR-0006：流式 run 的显式生命周期与有界回收

- 状态：已采纳
- 日期：2026-08-02

## 背景

LangGraph Checkpointer 只负责图状态持久化，不拥有 SSE 连接、producer 线程、run lease、
临时模型凭据或隔离工作区。过去这些资源由 SSE `finally` 分散释放：它只设置停止标志，
没有等待 producer，也通过一个布尔分支推测是否应保留 HITL 资源。断连与并发异常可能使
后台线程继续运行、工作区残留，或让旧请求重复清理新 run。

## 决策

每次逻辑 run 建立一个 `RunLifecycle`，并由进程级 `ActiveRunRegistry` 跟踪活动 run 与
待恢复 HITL run。清理操作幂等、对部分初始化安全，固定执行以下顺序：

1. 设置协作式停止信号；
2. 有界等待 producer 线程退出并获取其异常；
3. 撤销短期模型凭据；
4. 对非 HITL 终态删除 checkpoint；
5. 按 run_id 释放 lease；
6. 最后删除隔离工作区并解除任务注册。

同步 LangGraph/模型 SDK 运行在线程中，Python 不能安全强杀线程。producer 超时后，请求
不无限等待，但生命周期对象继续持有资源；受跟踪的延迟清理 task 在线程实际结束后按同一
顺序回收。这比提前删除仍可能被线程使用的工作区或凭据更安全。服务关闭会对注册表内所有
run 执行同样的有界停止流程。

## 终态与保留策略

| 终态 | producer | lease / credential | checkpoint | workspace |
| --- | --- | --- | --- | --- |
| 正常成功、普通失败、预算熔断 | 停止并等待 | 释放 / 撤销 | 删除 | 删除 |
| 客户端断连、SSE 关闭、请求取消 | 停止并等待 | 释放 / 撤销 | 删除 | 删除 |
| producer 异常或恢复失败 | 获取异常后结束 | 释放 / 撤销 | 删除 | 删除 |
| HITL 最终审批暂停 | producer 已结束 | 保留 lease、撤销本次凭据 | 保留 | 保留 |
| HITL 批准后完成或拒绝 | 停止并等待 | 释放 / 撤销 | 删除 | 删除 |
| 服务关闭 | 有界停止全部活动 run | 释放 / 撤销 | 删除 | 删除 |

HITL 保留不是“未走到成功分支”的副作用。只有 checkpoint 的 interrupt 类型是最终聚合
审批、活动 lease 与 approval_id 匹配、workspace_id 存在，且当前完整工作区摘要同时匹配
Reviewer 证据和最终审批快照时，才建立显式 `HitlRetention`。任何校验失败都按不可恢复
终态清理。

## 可观测性与安全

生命周期日志只包含 run_id、状态枚举、资源名、布尔结果和异常类型，可区分 disconnect、
cancellation、producer timeout、HITL 保留、workspace 清理、重复清理与清理失败。日志
禁止包含 Prompt、模型原始响应、API Key、凭据引用和源码全文。

## 权衡

- 断连默认取消当前非 HITL run；当前产品没有后台 run 的重新订阅协议，因此不把资源泄漏
  包装成“后台继续运行”。
- checkpoint 只为有效 HITL 恢复保留；完成或放弃的 run 会删除 checkpoint，牺牲无明确
  用途的历史状态保留，以换取确定的资源边界。
- 协作式线程停止受单次模型调用超时约束；极端不响应的第三方 SDK 只能延迟清理，无法在
  Python 进程内安全强杀。
