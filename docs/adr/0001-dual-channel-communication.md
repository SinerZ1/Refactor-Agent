# ADR-0001：SSE 与 WebSocket 双通道通信

- 状态：已采纳
- 日期：2026-07-29

## 背景

一次重构运行同时包含两种通信：

- 带完整请求体、持续较久、按顺序产生大量状态和工具事件的服务器到客户端流；
- A2A 聊天、人工审批通知和审批答复等低延迟双向交互。

单独使用浏览器 `EventSource` 无法 POST 模型和运行配置；单独使用 WebSocket 则需要自行重建请求取消、心跳、代理、错误和有序业务流语义。

## 决策

- 使用 `fetch()` POST `/api/refactor/stream` 并消费 `text/event-stream`。
- SSE 是 `run.*`、`plan.*`、`task.*`、`tool.*`、`review.*`、`approval.*` 的权威有序事件流。
- 使用 `/ws/refactor/{thread_id}` 承载 A2A 消息和审批请求/答复。
- 审批等待同时发出 SSE 事件和尽力而为的 WebSocket 通知。
- WebSocket 不可用时，客户端可通过 HTTP approval 端点提交决定。
- 两个通道都携带/校验 `run_id`，前端忽略旧 run 的迟到消息。

## 原因

该拆分遵循消息形态而不是 UI 功能拆分。SSE 保持运行事实流简单、可取消、易测试；WebSocket 保持交互低延迟。通知冗余不会降低审批门禁：没有有效 nonce 和明确决定时状态机仍保持挂起。

## 后果

正面：

- POST 请求体和流式响应可以共存；
- SSE 中断可直接释放 run lease 和临时凭据；
- WebSocket 短暂中断不会让前端丢失权威任务状态；
- 审批具备 HTTP 备用路径。

代价：

- 前端需要管理两种连接生命周期；
- 必须用 `run_id` 去重和过滤迟到消息；
- 代理部署必须同时支持流式响应和 WebSocket upgrade；
- 事件 schema 需要版本兼容。

## 被否决方案

- 仅 EventSource：不能自然携带 POST 请求体。
- 仅 WebSocket：把所有业务流、取消和重连协议都变成自定义实现。
- 轮询：延迟高、重复传输多，难以表现工具与任务的细粒度状态。
