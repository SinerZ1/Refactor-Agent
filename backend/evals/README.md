# Agent Eval

评测体系把“控制面是否安全、可审计”和“真实模型推理质量”分开测量：

- `offline` 使用 24 个脚本化 fake-model 场景，默认不创建供应商客户端、不调用付费
  API，适合本地和 CI 回归。
- `live` 必须显式选择供应商与模型，只授予只读判定 Prompt，不绑定文件或命令工具。
  凭据只能通过既有环境变量提供，命令行没有 `--api-key` 参数。

在 `backend/` 目录运行：

```powershell
venv/Scripts/python.exe -m evals.run --mode offline
```

报告生成到已忽略的 `evals/output/report.json` 与 `evals/output/report.md`。仓库中的
`evals/baselines/offline.json` 仅包含稳定的归一化指标和场景终态，不包含 Prompt、
源码全文、API 原始响应或任何凭据。完整离线运行会自动与该基准比较，发生漂移时返回
非零退出码。

真实模型模式可能产生费用，只有下面这种显式命令才会调用 API：

```powershell
venv/Scripts/python.exe -m evals.run --mode live `
  --provider openai `
  --model gpt-4o-mini
```

可重复使用 `--scenario <id>` 缩小评测范围。live 报告仍只保存白名单结构化字段及
Token 计量；解析失败只记录异常类型，不落盘供应商响应文本。
