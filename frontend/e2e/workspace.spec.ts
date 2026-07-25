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

  await expect(page.locator('.topology-container')).toHaveCount(0)

  await page.getByRole('button', { name: '任务 DAG' }).click()
  await expect(page.locator('.vue-flow')).toBeVisible()

  await page.getByRole('button', { name: '依赖图谱' }).click()
  await expect(page.getByText('代码架构拓扑图谱')).toBeVisible()
  await expect(page.getByText('AST 降级模式')).toBeVisible()
})
