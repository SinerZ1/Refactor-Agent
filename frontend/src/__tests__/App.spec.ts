import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import App from '../App.vue'

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
          file_path: 'CodeSmells/main.py',
          original_code: 'old',
          refactored_code: 'new',
        },
      }),
    } as MessageEvent)
    await wrapper.vm.$nextTick()
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
})
