import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { mount, type VueWrapper } from '@vue/test-utils'
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
})
