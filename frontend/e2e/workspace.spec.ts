import { expect, test } from '@playwright/test'

test.beforeEach(async ({ page }) => {
  await page.route('**/api/sessions', async (route) => {
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({
        thread_id: 'session_e2e',
        session_token: 'e2e-token',
      }),
    })
  })
  await page.route('**/api/models/adc-status', async (route) => {
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({
        available: false,
        message: 'E2E 环境未配置 ADC',
      }),
    })
  })
  await page.route('**/api/graph/topology', async (route) => {
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({
        nodes: [],
        links: [],
        fallback: true,
      }),
    })
  })
  await page.route('**/api/sessions/*/runs/*', async (route) => {
    await route.fulfill({ status: 404, contentType: 'application/json', body: '{}' })
  })
})

test('renders the refactor workspace and persists theme selection', async ({ page }) => {
  await page.goto('/')

  await expect(page.getByRole('heading', { name: 'Refactor Agent' })).toBeVisible()
  await expect(page.getByText('多轮交互重构对话')).toBeVisible()
  await expect(page.locator('.session-id')).toHaveText('session_e2e')

  await page.getByRole('button', { name: '深色' }).click()
  await expect(page.locator('.app-container')).toHaveAttribute('data-theme', 'dark')
  await expect
    .poll(() => page.evaluate(() => localStorage.getItem('refactor_agent_theme')))
    .toBe('dark')
})

test('connects a model provider without persisting its API key', async ({ page }) => {
  await page.route('**/api/models/connect', async (route) => {
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({
        models: ['deepseek-e2e'],
        message: '连接成功，共发现 1 个模型',
      }),
    })
  })
  await page.goto('/')

  await page.getByPlaceholder('sk-...').fill('e2e-secret')
  await page.getByRole('button', { name: '连接', exact: true }).click()

  await expect(page.locator('.connection-feedback')).toContainText('连接成功')
  await expect(page.locator('input[list="available-model-options"]')).toHaveValue('deepseek-e2e')
  const savedConfig = await page.evaluate(() => localStorage.getItem('refactor_agent_model_config'))
  expect(savedConfig).not.toContain('e2e-secret')
})

test('loads graph workspaces only after their tabs are selected', async ({ page }) => {
  await page.goto('/')
  await page.getByRole('button', { name: '浅色' }).click()

  await expect(page.locator('.topology-container')).toHaveCount(0)

  await page.getByRole('button', { name: '任务 DAG' }).click()
  await expect(page.locator('.vue-flow')).toBeVisible()

  await page.getByRole('button', { name: '依赖图谱' }).click()
  await expect(page.getByText('代码架构拓扑图谱')).toBeVisible()
  await expect(page.getByText('AST 降级模式')).toBeVisible()

  const topologyToolbar = page.locator('.topology-container .topology-toolbar')
  const topologyTitle = topologyToolbar.locator('.title')
  const fallbackBadge = topologyToolbar.locator('.topology-badge.fallback')
  const refreshButton = topologyToolbar.locator('.refresh-btn')
  await expect(topologyToolbar).toHaveCSS('background-color', 'rgb(247, 249, 253)')
  await expect(topologyTitle).toHaveCSS('color', 'rgb(66, 82, 106)')
  await expect(fallbackBadge).toHaveCSS('background-color', 'rgb(180, 83, 9)')
  await expect(refreshButton).toHaveCSS('background-color', 'rgb(237, 241, 248)')
  await expect(refreshButton).toHaveCSS('color', 'rgb(66, 82, 106)')
})

test('renders a dynamic DAG and authoritative budget from the SSE event chain', async ({
  page,
}) => {
  const runId = 'run_e2e'
  const event = (type: string, message: string, extra: Record<string, unknown> = {}) => ({
    version: 1,
    type,
    level: 'info',
    message,
    run_id: runId,
    ...extra,
  })
  const usage = {
    agent_steps: 2,
    tool_calls: 1,
    input_tokens: 900,
    output_tokens: 100,
    total_tokens: 1000,
    unmetered_steps: 0,
  }
  const limits = {
    max_agent_steps: 24,
    max_tool_calls: 32,
    max_total_tokens: 120_000,
    model_timeout_seconds: 60,
  }
  const events = [
    event('run.started', '开始'),
    event('plan.created', '计划完成', {
      level: 'success',
      task_id: 'architect_task',
      payload: {
        plan: {
          version: 1,
          summary: '先模型后入口',
          tasks: [
            {
              id: 'domain_models',
              title: '整理领域模型',
              description: '拆分数据对象',
              file_path: 'CodeSmells/models.py',
              dependencies: [],
            },
            {
              id: 'entrypoint',
              title: '调整入口编排',
              description: '接入领域模型',
              file_path: 'CodeSmells/main.py',
              dependencies: ['domain_models'],
            },
          ],
        },
      },
    }),
    event('run.usage.updated', '用量更新', {
      payload: { usage, limits },
    }),
    event('tool.completed', '模型文件写入完成', {
      level: 'success',
      task_id: 'domain_models',
      tool: 'write_code_file',
      success: true,
      payload: { file_path: 'CodeSmells/models.py' },
    }),
    event('task.completed', '任务完成：整理领域模型', {
      level: 'success',
      task_id: 'domain_models',
      payload: { retry_count: 0 },
    }),
    event('run.completed', '重构完成', { level: 'success', success: true }),
  ]
  await page.route('**/api/refactor/stream', async (route) => {
    await route.fulfill({
      contentType: 'text/event-stream',
      body: events.map((item) => `data: ${JSON.stringify(item)}\n\n`).join(''),
    })
  })

  await page.goto('/')
  await page.locator('.initial-btn').click()
  await page.getByRole('button', { name: '任务 DAG' }).click()

  await expect(page.getByText('🧩 整理领域模型')).toBeVisible()
  await expect(page.getByText('🧩 调整入口编排')).toBeVisible()
  await expect(page.getByText(/预算 2\/24 步 · 1\/32 工具 · 1,000\/120,000 Token/)).toBeVisible()
  await expect(
    page.locator('.vue-flow__node.dag-node-completed').filter({ hasText: '整理领域模型' }),
  ).toBeVisible()
})
