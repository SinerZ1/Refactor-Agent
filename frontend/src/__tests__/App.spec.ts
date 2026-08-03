import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import App from '../App.vue'
import WorkspaceTabs from '../components/WorkspaceTabs.vue'

class WebSocketStub {
  static readonly OPEN = 1
  static readonly CLOSED = 3
  static instances: WebSocketStub[] = []

  readonly url: string
  readyState = WebSocketStub.OPEN
  sentMessages: string[] = []
  onmessage: ((event: MessageEvent) => void) | null = null
  onclose: (() => void) | null = null
  onerror: ((event: Event) => void) | null = null

  constructor(url: string) {
    this.url = url
    WebSocketStub.instances.push(this)
  }

  close() {}
  send(message: string) {
    this.sentMessages.push(message)
  }
}

type SystemThemeListener = (event: MediaQueryListEvent) => void

const mountApp = () =>
  mount(App, {
    global: {
      stubs: {
        TopologyGraph: {
          template: '<div></div>',
          methods: {
            refresh: () => undefined,
            resize: () => undefined,
          },
        },
        VueFlow: true,
      },
    },
  })

describe('App', () => {
  let wrapper: VueWrapper | null = null
  let systemThemeListener: SystemThemeListener | null = null
  let systemDark = false

  beforeEach(() => {
    localStorage.clear()
    systemDark = false
    systemThemeListener = null
    WebSocketStub.instances = []

    vi.stubGlobal('WebSocket', WebSocketStub)
    vi.stubGlobal(
      'fetch',
      vi.fn<(input: RequestInfo | URL, init?: RequestInit) => Promise<Response>>(async () =>
        Promise.resolve(
          new Response(JSON.stringify({ available: false, message: '未找到有效 ADC 凭据文件' }), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          }),
        ),
      ),
    )
    vi.mocked(fetch).mockImplementation(async (input) => {
      if (String(input).endsWith('/api/sessions')) {
        return new Response(
          JSON.stringify({ thread_id: 'session_test', session_token: 'session-token' }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        )
      }
      return new Response(
        JSON.stringify({ available: false, message: '未找到有效 ADC 凭据文件' }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      )
    })
    Object.defineProperty(window, 'matchMedia', {
      configurable: true,
      value: vi.fn<(query: string) => MediaQueryList>(() => {
        return {
          get matches() {
            return systemDark
          },
          media: '(prefers-color-scheme: dark)',
          onchange: null,
          addEventListener: (_type: string, listener: SystemThemeListener) => {
            systemThemeListener = listener
          },
          removeEventListener: () => {
            systemThemeListener = null
          },
          addListener: () => undefined,
          removeListener: () => undefined,
          dispatchEvent: () => true,
        } as MediaQueryList
      }),
    })
  })

  afterEach(() => {
    wrapper?.unmount()
    wrapper = null
    vi.unstubAllGlobals()
  })

  it('renders the conversation-focused workspace', () => {
    wrapper = mountApp()

    expect(wrapper.get('h1').text()).toBe('Refactor Agent')
    expect(wrapper.text()).toContain('多轮交互重构对话')
    expect(wrapper.find('.main-content').exists()).toBe(true)
    const legacyTitleIcons = ['⚙️', '💾', '💬', '🛠️']
    expect(
      wrapper
        .findAll('.panel-header h3')
        .every((title) => legacyTitleIcons.every((icon) => !title.text().includes(icon))),
    ).toBe(true)
  })

  it('switches themes and persists the explicit preference', async () => {
    wrapper = mountApp()

    expect(wrapper.get('.app-container').attributes('data-theme')).toBe('light')

    const darkButton = wrapper
      .findAll('.theme-option')
      .find((button) => button.text().includes('深色'))
    expect(darkButton).toBeDefined()
    await darkButton!.trigger('click')

    expect(wrapper.get('.app-container').attributes('data-theme')).toBe('dark')
    expect(localStorage.getItem('refactor_agent_theme')).toBe('dark')
  })

  it('reacts to operating-system changes while following the system theme', async () => {
    wrapper = mountApp()

    systemDark = true
    systemThemeListener?.({ matches: true } as MediaQueryListEvent)
    await wrapper.vm.$nextTick()

    expect(wrapper.get('.app-container').attributes('data-theme')).toBe('dark')
  })

  it('connects to the selected provider and fills the model field', async () => {
    vi.mocked(fetch).mockImplementation(async (input) => {
      const url = String(input)
      if (url.endsWith('/api/sessions')) {
        return new Response(
          JSON.stringify({ thread_id: 'session_test', session_token: 'session-token' }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        )
      }
      if (url.endsWith('/api/models/connect')) {
        return new Response(
          JSON.stringify({ models: ['deepseek-v4-pro'], message: '连接成功，共发现 1 个模型' }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        )
      }
      return new Response(
        JSON.stringify({ available: false, message: '未找到有效 ADC 凭据文件' }),
        {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        },
      )
    })
    wrapper = mountApp()

    const apiKeyInput = wrapper.get('input[placeholder="sk-..."]')
    await apiKeyInput.setValue('test-key')
    await wrapper.get('.connect-btn').trigger('click')
    await flushPromises()

    expect(wrapper.get('input[list="available-model-options"]').element).toHaveProperty(
      'value',
      'deepseek-v4-pro',
    )
    expect(wrapper.get('.connection-feedback').text()).toContain('连接成功')
    expect(localStorage.getItem('refactor_agent_model_config')).not.toContain('test-key')
  })

  it('authenticates the websocket with the backend-issued session', async () => {
    wrapper = mountApp()
    await flushPromises()

    expect(WebSocketStub.instances).toHaveLength(1)
    expect(WebSocketStub.instances[0]!.url).toContain('/ws/refactor/session_test')
    expect(WebSocketStub.instances[0]!.url).toContain('token=session-token')
  })

  it('submits approval through REST when websocket is disconnected', async () => {
    vi.mocked(fetch).mockImplementation(async (input) => {
      const url = String(input)
      if (url.endsWith('/api/sessions')) {
        return new Response(
          JSON.stringify({ thread_id: 'session_test', session_token: 'session-token' }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        )
      }
      if (url.includes('/approval')) {
        return new Response(JSON.stringify({ approval_id: 'approval-1', accepted: true }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        })
      }
      if (url.endsWith('/api/refactor/stream')) {
        return new Response('', { status: 200 })
      }
      return new Response(
        JSON.stringify({ available: false, message: '未找到有效 ADC 凭据文件' }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      )
    })
    wrapper = mountApp()
    await flushPromises()

    const webSocket = WebSocketStub.instances[0]!
    webSocket.onmessage?.({
      data: JSON.stringify({
        type: 'approval_request',
        payload: {
          approval_id: 'approval-1',
          type: 'aggregate_diff_approval',
          file_path: '聚合变更（2 个文件）',
          original_code: '',
          refactored_code: '-old\n+new',
          aggregate_diff: '-old\n+new',
          changed_files: ['CodeSmells/main.py', 'CodeSmells/models.py'],
        },
      }),
    } as MessageEvent)
    await wrapper.vm.$nextTick()
    expect(wrapper.text()).toContain('最终聚合变更确认')
    expect(wrapper.text()).toContain('2 个文件的完整聚合 diff')
    expect(wrapper.get('.diff-pre').text()).toContain('-old')
    webSocket.readyState = WebSocketStub.CLOSED
    await wrapper.get('.btn-approve').trigger('click')
    await flushPromises()

    const approvalCall = vi
      .mocked(fetch)
      .mock.calls.find(([input]) => String(input).includes('/approval'))
    expect(approvalCall).toBeDefined()
    expect(JSON.parse(String(approvalCall![1]?.body))).toEqual({
      session_token: 'session-token',
      approval_id: 'approval-1',
      approved: true,
    })
  })

  it('aborts the previous stream before creating a new session', async () => {
    let sessionNumber = 0
    let streamSignal: AbortSignal | null = null
    vi.mocked(fetch).mockImplementation(async (input, init) => {
      const url = String(input)
      if (url.endsWith('/api/sessions')) {
        sessionNumber += 1
        return new Response(
          JSON.stringify({
            thread_id: `session_${sessionNumber}`,
            session_token: `token_${sessionNumber}`,
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        )
      }
      if (url.endsWith('/api/refactor/stream')) {
        streamSignal = init?.signal ?? null
        return await new Promise<Response>((_resolve, reject) => {
          streamSignal?.addEventListener('abort', () => {
            reject(new DOMException('aborted', 'AbortError'))
          })
        })
      }
      return new Response(
        JSON.stringify({ available: false, message: '未找到有效 ADC 凭据文件' }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      )
    })
    wrapper = mountApp()
    await flushPromises()

    await wrapper.get('.initial-btn').trigger('click')
    await flushPromises()
    expect(streamSignal).not.toBeNull()

    await wrapper.get('.new-session-btn').trigger('click')
    await flushPromises()

    expect((streamSignal as AbortSignal | null)?.aborted).toBe(true)
    expect(wrapper.get('.session-id').text()).toBe('session_2')
    expect(wrapper.findAll('.log-item.error')).toHaveLength(0)
  })

  it('renders markdown formatted content in chat messages correctly', async () => {
    wrapper = mountApp()
    await flushPromises()

    const webSocket = WebSocketStub.instances[0]!
    webSocket.onmessage?.({
      data: JSON.stringify({
        type: 'chatroom_message',
        sender: 'CoderAgent',
        content:
          '**重构建议**: 请使用 `calculate()` 函数。\n```python\ndef calculate():\n    return 42\n```',
      }),
    } as MessageEvent)
    await wrapper.vm.$nextTick()

    const bubbleMarkdown = wrapper.find('.bubble-markdown')
    expect(bubbleMarkdown.exists()).toBe(true)
    expect(bubbleMarkdown.html()).toContain('<strong>重构建议</strong>')
    expect(bubbleMarkdown.html()).toContain('<code>calculate()</code>')
    expect(bubbleMarkdown.find('pre code').exists()).toBe(true)
    expect(bubbleMarkdown.find('pre code').text()).toContain('def calculate()')
  })

  it('ignores websocket messages from a stale run after SSE selects the active run', async () => {
    vi.mocked(fetch).mockImplementation(async (input) => {
      const url = String(input)
      if (url.endsWith('/api/sessions')) {
        return new Response(
          JSON.stringify({ thread_id: 'session_test', session_token: 'session-token' }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        )
      }
      if (url.endsWith('/api/refactor/stream')) {
        const started = {
          version: 1,
          type: 'run.started',
          level: 'info',
          message: '开始',
          run_id: 'run_current',
        }
        return new Response(`data: ${JSON.stringify(started)}\n\n`, {
          status: 200,
          headers: { 'Content-Type': 'text/event-stream' },
        })
      }
      return new Response(
        JSON.stringify({ available: false, message: '未找到有效 ADC 凭据文件' }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      )
    })
    wrapper = mountApp()
    await flushPromises()
    await wrapper.get('.initial-btn').trigger('click')
    await flushPromises()

    const webSocket = WebSocketStub.instances[0]!
    webSocket.onmessage?.({
      data: JSON.stringify({
        type: 'chatroom_message',
        run_id: 'run_stale',
        sender: 'CoderAgent',
        content: '过期运行消息',
      }),
    } as MessageEvent)
    webSocket.onmessage?.({
      data: JSON.stringify({
        type: 'chatroom_message',
        run_id: 'run_current',
        sender: 'CoderAgent',
        content: '当前运行消息',
      }),
    } as MessageEvent)
    await wrapper.vm.$nextTick()

    expect(wrapper.text()).not.toContain('过期运行消息')
    expect(wrapper.text()).toContain('当前运行消息')
  })

  it('resets all run-scoped panels and rejects late events without clearing user settings', async () => {
    let sessionNumber = 0
    const plan = {
      version: 1,
      summary: '旧运行计划',
      tasks: [
        {
          id: 'old_task',
          title: '旧任务',
          description: '不应进入新会话',
          file_path: 'CodeSmells/old.py',
          dependencies: [],
        },
      ],
    }
    const usage = {
      agent_steps: 2,
      tool_calls: 1,
      input_tokens: 100,
      output_tokens: 20,
      total_tokens: 120,
      unmetered_steps: 0,
    }
    const limits = {
      max_agent_steps: 2,
      max_tool_calls: 1,
      max_total_tokens: 120,
      model_timeout_seconds: 30,
    }
    const applyFailure = {
      code: 'workspace_apply_failure',
      phase: 'rollback',
      conflict_category: 'third_party_change',
      rollback_status: 'partial',
      requires_manual_action: true,
      affected_file_count: 1,
      affected_files: ['CodeSmells/old.py'],
      recovery_available: true,
      recovery_ids: ['recovery-safe-id'],
      guidance: '请人工核对后再重试。',
    }
    const events = [
      {
        version: 1,
        type: 'run.started',
        level: 'info',
        message: '旧运行开始',
        run_id: 'run-old',
      },
      {
        version: 1,
        type: 'plan.created',
        level: 'success',
        message: '旧计划创建',
        run_id: 'run-old',
        payload: { plan },
      },
      {
        version: 1,
        type: 'run.budget.exceeded',
        level: 'error',
        message: '旧预算耗尽',
        run_id: 'run-old',
        payload: { usage, limits, reason: '旧预算耗尽' },
      },
      {
        version: 1,
        type: 'workspace.apply.failed',
        level: 'error',
        message: '旧应用失败',
        run_id: 'run-old',
        payload: { apply_failure: applyFailure },
      },
      {
        version: 1,
        type: 'run.lifecycle.updated',
        level: 'error',
        message: '旧清理失败',
        run_id: 'run-old',
        payload: {
          snapshot: {
            version: 1,
            run_id: 'run-old',
            lifecycle_status: 'cleanup_failed',
            terminal_status: 'failed',
            termination_reason: 'producer_timeout',
            workspace_retained: false,
            cleanup_errors: ['deferred_cleanup_failed'],
            apply_failure: applyFailure,
          },
        },
      },
      {
        version: 1,
        type: 'approval.waiting',
        level: 'info',
        message: '旧审批等待',
        run_id: 'run-old',
        payload: {
          approval_id: 'approval-old',
          type: 'aggregate_diff_approval',
          file_path: '聚合变更（1 个文件）',
          original_code: '',
          refactored_code: '-old\n+new',
          aggregate_diff: '-old\n+new',
          changed_files: ['CodeSmells/old.py'],
        },
      },
      {
        version: 1,
        type: 'run.lifecycle.updated',
        level: 'error',
        message: '旧清理终态',
        run_id: 'run-old',
        payload: {
          snapshot: {
            version: 1,
            run_id: 'run-old',
            lifecycle_status: 'cleanup_failed',
            terminal_status: 'failed',
            termination_reason: 'producer_timeout',
            workspace_retained: false,
            cleanup_errors: ['deferred_cleanup_failed'],
            apply_failure: applyFailure,
          },
        },
      },
    ]
    vi.mocked(fetch).mockImplementation(async (input) => {
      const url = String(input)
      if (url.endsWith('/api/sessions')) {
        sessionNumber += 1
        return new Response(
          JSON.stringify({
            thread_id: `session_${sessionNumber}`,
            session_token: `token_${sessionNumber}`,
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        )
      }
      if (url.endsWith('/api/refactor/stream')) {
        return new Response(events.map((event) => `data: ${JSON.stringify(event)}\n\n`).join(''), {
          status: 200,
          headers: { 'Content-Type': 'text/event-stream' },
        })
      }
      if (url.includes('/runs/')) return new Response('', { status: 404 })
      return new Response(
        JSON.stringify({ available: false, message: '未找到有效 ADC 凭据文件' }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      )
    })
    wrapper = mountApp()
    await flushPromises()
    const darkButton = wrapper
      .findAll('.theme-option')
      .find((button) => button.text().includes('深色'))!
    await darkButton.trigger('click')
    const modelInput = wrapper.get('input[list="available-model-options"]')
    await modelInput.setValue('keep-this-model')

    await wrapper.get('.initial-btn').trigger('click')
    await flushPromises()
    const workspace = wrapper.findComponent(WorkspaceTabs)
    expect((workspace.props('nodes') as unknown[]).length).toBeGreaterThan(0)
    expect(wrapper.text()).toContain('旧预算耗尽')
    expect(wrapper.text()).toContain('清理失败')
    expect(wrapper.text()).toContain('未能完整恢复')
    expect(wrapper.text()).toContain('最终聚合变更确认')
    const oldSocket = WebSocketStub.instances[0]!
    const queuedOldMessage = oldSocket.onmessage!

    await wrapper.get('.new-session-btn').trigger('click')
    await flushPromises()
    expect(workspace.props('nodes')).toEqual([])
    expect(workspace.props('edges')).toEqual([])
    expect(workspace.props('budgetUsage')).toBeNull()
    expect(workspace.props('runStatus')).toBeNull()
    expect(wrapper.find('.modal-overlay').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('旧预算耗尽')
    expect(wrapper.text()).not.toContain('未能完整恢复')

    queuedOldMessage({
      data: JSON.stringify({
        type: 'approval_request',
        run_id: 'run-old',
        payload: {
          approval_id: 'late-old',
          file_path: '旧审批',
          original_code: 'old',
          refactored_code: 'new',
        },
      }),
    } as MessageEvent)
    expect(wrapper.find('.modal-overlay').exists()).toBe(false)

    const newSocket = WebSocketStub.instances[1]!
    newSocket.onmessage?.({
      data: JSON.stringify({
        type: 'chatroom_message',
        run_id: 'run-new',
        sender: 'CoderAgent',
        content: '新会话消息',
      }),
    } as MessageEvent)
    await wrapper.vm.$nextTick()
    expect(wrapper.text()).toContain('新会话消息')
    expect(wrapper.get('.app-container').attributes('data-theme')).toBe('dark')
    expect(modelInput.element).toHaveProperty('value', 'keep-this-model')

    await wrapper.get('.new-session-btn').trigger('click')
    await flushPromises()
    expect(wrapper.get('.session-id').text()).toBe('session_3')
    expect(wrapper.findAll('.log-item.error')).toHaveLength(0)
    expect(workspace.props('nodes')).toEqual([])
  })
})
