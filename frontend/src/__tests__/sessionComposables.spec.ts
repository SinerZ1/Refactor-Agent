import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ref } from 'vue'

import { useAgentChat } from '../composables/useAgentChat'
import { useApprovalFlow } from '../composables/useApprovalFlow'
import { useBackendSession } from '../composables/useBackendSession'
import { useRefactorStream } from '../composables/useRefactorStream'
import type { AgentEvent } from '../types/agentEvents'
import type { BackendMessage } from '../types/workspace'

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

  close() {
    this.readyState = WebSocketStub.CLOSED
  }

  send(message: string) {
    this.sentMessages.push(message)
  }
}

describe('session and stream composables', () => {
  beforeEach(() => {
    WebSocketStub.instances = []
    vi.stubGlobal('WebSocket', WebSocketStub)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('owns chat input and maps backend agent roles without leaking protocol logic to components', () => {
    const chat = useAgentChat()

    chat.userChatInput.value = '  继续重构  '
    expect(chat.takeUserMessage()).toBe('继续重构')
    expect(chat.chatMessages.value[0]).toEqual({ role: 'user', text: '继续重构' })

    expect(
      chat.handleBackendMessage({
        type: 'chatroom_message',
        sender: 'ReviewerAgent',
        content: '测试已通过',
      }),
    ).toBe(true)
    expect(chat.chatMessages.value[1]).toEqual({ role: 'reviewer', text: '测试已通过' })
  })

  it('creates an authenticated websocket and rejects messages from stale runs', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        new Response(
          JSON.stringify({ thread_id: 'session-one', session_token: 'session-token' }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        ),
      ),
    )
    const activeRunId = ref('run-current')
    const handler = vi.fn<(message: BackendMessage) => void>()
    const session = useBackendSession({
      activeRunId,
      onError: vi.fn<(message: string) => void>(),
    })
    session.setMessageHandler(handler)

    await session.startBackendSession()
    const webSocket = WebSocketStub.instances[0]!
    expect(webSocket.url).toContain('/ws/refactor/session-one')
    expect(webSocket.url).toContain('token=session-token')

    webSocket.onmessage?.({
      data: JSON.stringify({ type: 'chatroom_message', run_id: 'run-stale' }),
    } as MessageEvent)
    webSocket.onmessage?.({
      data: JSON.stringify({ type: 'chatroom_message', run_id: 'run-current' }),
    } as MessageEvent)

    expect(handler).toHaveBeenCalledTimes(1)
    expect(handler).toHaveBeenCalledWith(
      expect.objectContaining({ run_id: 'run-current' }),
    )
  })

  it('falls back to the approval REST endpoint when the websocket is closed', async () => {
    const fetchMock = vi.fn<
      (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>
    >(
      async (_input: RequestInfo | URL, _init?: RequestInit) =>
        new Response(JSON.stringify({ approval_id: 'approval-one', accepted: true }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
    )
    vi.stubGlobal('fetch', fetchMock)
    const closedSocket = new WebSocketStub('ws://test')
    closedSocket.readyState = WebSocketStub.CLOSED
    const resume = vi.fn<() => Promise<void>>(async () => undefined)
    const approval = useApprovalFlow({
      threadId: ref('session-one'),
      sessionToken: ref('session-token'),
      socket: ref(closedSocket as unknown as WebSocket),
      onResume: resume,
      onError: vi.fn<(message: string) => void>(),
    })

    approval.handleStreamApproval({
      approval_id: 'approval-one',
      file_path: 'CodeSmells/main.py',
      original_code: 'old',
      refactored_code: 'new',
    })
    await approval.submitApprovalDecision(true)

    expect(JSON.parse(String(fetchMock.mock.calls[0]![1]?.body))).toEqual({
      session_token: 'session-token',
      approval_id: 'approval-one',
      approved: true,
    })
    expect(approval.isApprovalModalOpen.value).toBe(false)
    expect(resume).toHaveBeenCalledOnce()
  })

  it('parses SSE envelopes and filters stale run events inside the stream boundary', async () => {
    const events = [
      {
        version: 1,
        type: 'run.started',
        level: 'info',
        message: '开始',
        run_id: 'run-current',
      },
      {
        version: 1,
        type: 'log',
        level: 'info',
        message: '旧运行日志',
        run_id: 'run-stale',
      },
      {
        version: 1,
        type: 'agent.message.delta',
        level: 'info',
        message: '当前响应',
        run_id: 'run-current',
      },
    ]
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        new Response(events.map((event) => `data: ${JSON.stringify(event)}\n\n`).join(''), {
          status: 200,
          headers: { 'Content-Type': 'text/event-stream' },
        }),
      ),
    )
    const appliedEvents: AgentEvent[] = []
    const logs: string[] = []
    const responseText = ref('')
    const stream = useRefactorStream({
      activeRunId: ref(''),
      threadId: ref('session-one'),
      sessionToken: ref('session-token'),
      ensureBackendSession: async () => undefined,
      getRuntimeModelConfig: () => ({}),
      beginAgentResponse: () => 0,
      updateAgentResponse: (_index, text) => {
        responseText.value = text
      },
      resetInitialTurn: () => undefined,
      applyEvent: (event) => appliedEvents.push(event),
      onApproval: () => undefined,
      onLog: (log) => logs.push(log.message),
      onPlanCreated: () => undefined,
      onStreamSettled: () => undefined,
    })

    await stream.sendStreamRequest('CodeSmells/main.py', true)

    expect(appliedEvents.map((event) => event.run_id)).toEqual(['run-current', 'run-current'])
    expect(logs).toEqual(['开始'])
    expect(responseText.value).toBe('当前响应')
    expect(stream.isRefactoring.value).toBe(false)
  })
})
