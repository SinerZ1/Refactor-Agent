# Agent 评测说明

Refactor-Agent 的 Eval 将“确定性控制面是否正确”与“真实模型是否给出高质量判断”分开。CI 只运行离线 fake-model 模式，避免网络、费用和供应商波动掩盖状态机回归。

## 1. 评测目标

评测回答五类问题：

1. Agent 能否针对常见 Python 坏味道完成预期工作流？
2. Reviewer 是否只在最新测试证据有效时成功？
3. Prompt injection、越界路径与命令注入是否被确定性边界阻断？
4. 预算耗尽、测试失败、审批拒绝和多文件失败是否进入正确终态？
5. 报告是否只保留可审计指标，不泄露 Prompt、源码、供应商响应或凭据？

## 2. 模式

| 模式 | 模型 | 网络/费用 | 用途 |
| --- | --- | --- | --- |
| `offline` | `scripted-fake-model-v1` | 无 | CI、回归、基准比较 |
| `live` | 显式供应商与模型 | 可能有 | 可选推理质量抽样 |

离线模式包含 24 个固定场景和 192 个行为检查。场景定义在 `backend/evals/scenarios.py`，执行器位于 `backend/evals/runner.py`，基准位于 `backend/evals/baselines/offline.json`。

## 3. 场景分层

| 类别 | 代表场景 | 主要断言 |
| --- | --- | --- |
| 代码质量 | 长函数、重复验证、命名、类型、异常边界 | 任务、工具、审查与预期成功终态 |
| 跨文件 DAG | 接口拆分、依赖顺序 | 拓扑顺序、任务状态、跨文件变更 |
| Prompt injection | 源码注释注入、用户请求注入 | 策略阻断、无越界工具副作用 |
| 路径安全 | 父目录逃逸、绝对路径 | 写入被拒绝、源码保持不变 |
| HITL | 单文件/多文件拒绝 | 审批拒绝终态、工作区清理 |
| Reviewer 失败 | 测试失败、证据陈旧、协议错误 | 不得进入成功，按策略重试/失败 |
| 预算熔断 | 步数、工具、Token、超时 | 明确预算终态、无候选应用 |

离线场景的“通过”表示系统到达该场景预期终态。例如安全攻击应以 `safety_blocked` 通过，用户拒绝应以 `approval_rejected` 通过。因此“场景通过率”不能与“最终重构成功率”混为一谈。

## 4. 当前基准

基准版本：schema 1，模型 `scripted-fake-model-v1`。

| 指标 | 值 | 解读 |
| --- | ---: | --- |
| 场景数 | 24 | 固定离线集合 |
| 场景通过 | 24（100%） | 全部达到预期终态 |
| 行为检查 | 192/192（100%） | 所有结构化断言通过 |
| 首次审查成功率 | 77.78% | 衡量无 Reviewer 重试的比例 |
| 最终成功率 | 62.50% | 其余场景包含故意失败/阻断/拒绝 |
| 平均重试 | 0.25 | 每场景平均重试次数 |
| 工具调用数 | 136 | 全集合的白名单工具调用 |
| 输入 Token | 21,190 | 脚本模型的统一计量 |
| 输出 Token | 6,030 | 脚本模型的统一计量 |
| 总 Token | 27,220 | 预算与趋势比较用 |
| HITL 拒绝率 | 11.76% | 集合内预设拒绝比例 |
| 安全策略阻断率 | 100% | 预期攻击全部被阻断 |
| 模型错误数 | 0 | 无非预期适配器错误 |

这些数字是控制面回归基准，不应当作为真实模型在未知仓库上的准确率宣传。

## 5. 运行离线 Eval

必须在 `backend/` 目录使用虚拟环境：

```powershell
venv\Scripts\python.exe -m evals.run --mode offline
```

运行结果写到：

```text
backend/evals/output/
  report.json
  report.md
```

`output/` 已忽略。完整离线运行会与版本控制中的 `baselines/offline.json` 比较；任何稳定字段漂移都会以非零退出码失败。耗时等天然波动字段不进入严格比较。

只运行特定场景用于诊断：

```powershell
venv\Scripts\python.exe -m evals.run --mode offline `
  --scenario prompt-injection-source-comment
```

单场景运行不会被错误地当成完整基准更新。

## 6. 可选 live Eval

live 模式必须显式提供供应商和模型：

```powershell
venv\Scripts\python.exe -m evals.run --mode live `
  --provider openai `
  --model <model-name> `
  --scenario long-function-extract
```

约束：

- 命令行没有 `--api-key` 参数；
- 凭据只从既有环境变量读取；
- live 模型只执行只读判定，不绑定文件或命令工具；
- 运行可能产生费用；
- 供应商网络错误不会被包装为离线基准通过；
- live 报告仍只保存白名单结构字段和 Token 计量。

运行 live 模式前应先执行 offline，确认控制面没有回归。

## 7. 报告隐私边界

允许落盘：

- 场景 ID、模式、模型标识；
- 预期/实际终态；
- 布尔行为检查；
- 重试、工具、Token、耗时、HITL 和安全阻断指标；
- 脱敏错误类别。

禁止落盘：

- System/Human Prompt；
- `CodeSmells/` 源码全文或完整 diff；
- 模型原始响应；
- API Key、session token、approval nonce；
- Redis/Neo4j 凭据；
- 本机 ADC 绝对路径。

对应白名单序列化由 `backend/evals/reporting.py` 负责，相关测试位于 `backend/tests/test_evals.py`。

## 8. 基准变更流程

只有在预期行为有意变化时才更新基准：

1. 先运行相关单元测试定位行为变化；
2. 检查每个发生变化的场景终态和行为断言；
3. 确认没有安全阻断率下降、证据门禁放宽或凭据字段新增；
4. 完整运行 offline；
5. 只提交规范化基准，不提交 `evals/output/`；
6. 在提交说明中解释为什么指标变化是预期结果。

不得为了让 CI 变绿而删除攻击、失败、预算或 HITL 拒绝场景。

## 9. 指标限制与后续方向

- scripted fake model 衡量控制面可重复性，不衡量自然语言泛化。
- 当前集合规模适合 PR 回归，不构成统计显著的模型排行榜。
- Token 是统一脚本计量，可比较回归趋势，但不等同于每个供应商的账单。
- 首次审查成功率会受到场景中“故意要求重试”的占比影响。
- 后续可增加真实历史补丁、变异测试、语义等价性 oracle 和多模型盲测，但必须继续保持凭据与源码不进入报告。
