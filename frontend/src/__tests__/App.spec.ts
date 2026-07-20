import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import App from '../App.vue'

class WebSocketStub {
  static readonly OPEN = 1
  readyState = WebSocketStub.OPEN
  onmessage: ((event: MessageEvent) => void) | null = null
  onclose: (() => void) | null = null
  onerror: ((event: Event) => void) | null = null

  close() {}
  send() {}
}

type SystemThemeListener = (event: MediaQueryListEvent) => void

const mountApp = () =>
  mount(App, {
    global: {
      stubs: {
        TopologyGraph: true,
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
})
