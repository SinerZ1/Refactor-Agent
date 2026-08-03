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

  it('invalidates queued websocket callbacks when a session is reset or disposed', async () => {
    let sessionNumber = 0
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        sessionNumber += 1
        return new Response(
          JSON.stringify({
            thread_id: `session-${sessionNumber}`,
            session_token: `token-${sessionNumber}`,
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        )
      }),
    )
    const activeRunId = ref('')
    const handler = vi.fn<(message: BackendMessage) => void>()
    const session = useBackendSession({
      activeRunId,
      onError: vi.fn<(message: string) => void>(),
    })
    session.setMessageHandler(handler)

    await session.startBackendSession()
    const oldSocket = WebSocketStub.instances[0]!
    const queuedOldCallback = oldSocket.onmessage!
    await session.resetBackendSession()
    queuedOldCallback({
      data: JSON.stringify({ type: 'approval_request', run_id: 'run-old' }),
    } as MessageEvent)
    expect(handler).not.toHaveBeenCalled()
    expect(activeRunId.value).toBe('')

    const currentSocket = WebSocketStub.instances[1]!
    const queuedCurrentCallback = currentSocket.onmessage!
    currentSocket.onmessage?.({
      data: JSON.stringify({ type: 'chatroom_message', run_id: 'run-new' }),
    } as MessageEvent)
    expect(handler).toHaveBeenCalledOnce()
    expect(activeRunId.value).toBe('run-new')

    session.disposeBackendSession()
    queuedCurrentCallback({
      data: JSON.stringify({ type: 'chatroom_message', run_id: 'run-new' }),
    } as MessageEvent)
    expect(handler).toHaveBeenCalledOnce()
  })

  it('keeps only the newest backend session when resets overlap', async () => {
    const resolvers: Array<(response: Response) => void> = []
    vi.stubGlobal(
      'fetch',
      vi.fn(
        () =>
          new Promise<Response>((resolve) => {
            resolvers.push(resolve)
          }),
      ),
    )
    const session = useBackendSession({
      activeRunId: ref(''),
      onError: vi.fn<(message: string) => void>(),
    })

    const first = session.resetBackendSession()
    const second = session.resetBackendSession()
    resolvers[1]!(
      new Response(JSON.stringify({ thread_id: 'newest', session_token: 'newest-token' }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    )
    await second
    resolvers[0]!(
      new Response(JSON.stringify({ thread_id: 'stale', session_token: 'stale-token' }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    )
    await first

    expect(session.threadId.value).toBe('newest')
    expect(WebSocketStub.instances).toHaveLength(1)
    expect(WebSocketStub.instances[0]!.url).toContain('/ws/refactor/newest')
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

  it('does not resume a new session from a late approval HTTP response', async () => {
    let resolveApproval!: (response: Response) => void
    vi.stubGlobal(
      'fetch',
      vi.fn<() => Promise<Response>>(
        () =>
          new Promise<Response>((resolve) => {
            resolveApproval = resolve
          }),
      ),
    )
    const closedSocket = new WebSocketStub('ws://test')
    closedSocket.readyState = WebSocketStub.CLOSED
    const resume = vi.fn<() => Promise<void>>(async () => undefined)
    const approval = useApprovalFlow({
      threadId: ref('session-old'),
      sessionToken: ref('token-old'),
      socket: ref(closedSocket as unknown as WebSocket),
      onResume: resume,
      onError: vi.fn<(message: string) => void>(),
    })
    approval.handleStreamApproval({
      approval_id: 'approval-old',
      file_path: 'CodeSmells/old.py',
      original_code: 'old',
      refactored_code: 'new',
    })

    const pending = approval.submitApprovalDecision(true)
    approval.resetApproval()
    resolveApproval(
      new Response(JSON.stringify({ approval_id: 'approval-old', accepted: true }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    )
    await pending

    expect(resume).not.toHaveBeenCalled()
    expect(approval.approvalPayload.value).toBeNull()
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
      beginAgentResponse: () => ({ generation: 0, index: 0 }),
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

  it('drops queued plan and task events after stream reset while accepting the new session', async () => {
    let oldController: ReadableStreamDefaultController<Uint8Array> | null = null
    let requestNumber = 0
    const encoder = new TextEncoder()
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        requestNumber += 1
        if (requestNumber === 1) {
          return new Response(
            new ReadableStream<Uint8Array>({
              start(controller) {
                oldController = controller
              },
            }),
            { status: 200, headers: { 'Content-Type': 'text/event-stream' } },
          )
        }
        const newPlan = {
          version: 1,
          type: 'plan.created',
          level: 'success',
          message: '新计划',
          run_id: 'run-new',
          payload: {
            plan: {
              version: 1,
              summary: '新会话计划',
              tasks: [
                {
                  id: 'new_task',
                  title: '新任务',
                  description: '只属于新会话',
                  file_path: 'CodeSmells/new.py',
                  dependencies: [],
                },
              ],
            },
          },
        }
        return new Response(`data: ${JSON.stringify(newPlan)}\n\n`, {
          status: 200,
          headers: { 'Content-Type': 'text/event-stream' },
        })
      }),
    )
    const threadId = ref('session-old')
    const sessionToken = ref('token-old')
    const applied: AgentEvent[] = []
    const chat = useAgentChat()
    const stream = useRefactorStream({
      activeRunId: ref(''),
      threadId,
      sessionToken,
      ensureBackendSession: async () => undefined,
      getRuntimeModelConfig: () => ({}),
      beginAgentResponse: chat.beginAgentResponse,
      updateAgentResponse: chat.updateAgentResponse,
      resetInitialTurn: chat.resetChat,
      applyEvent: (event) => applied.push(event),
      onApproval: () => undefined,
      onLog: () => undefined,
      onPlanCreated: () => undefined,
      onStreamSettled: () => undefined,
    })

    const oldRequest = stream.sendStreamRequest('old', true)
    await Promise.resolve()
    const oldPlan = {
      version: 1,
      type: 'plan.created',
      level: 'success',
      message: '旧计划',
      run_id: 'run-old',
      payload: {
        plan: {
          version: 1,
          summary: '旧会话计划',
          tasks: [
            {
              id: 'old_task',
              title: '旧任务',
              description: '不得泄漏',
              file_path: 'CodeSmells/old.py',
              dependencies: [],
            },
          ],
        },
      },
    }
    const oldTask = {
      version: 1,
      type: 'task.started',
      level: 'info',
      message: '旧任务开始',
      run_id: 'run-old',
      task_id: 'old_task',
    }
    oldController!.enqueue(
      encoder.encode(
        `data: ${JSON.stringify(oldPlan)}\n\ndata: ${JSON.stringify(oldTask)}\n\n`,
      ),
    )
    stream.resetStream()
    chat.resetChat()
    threadId.value = 'session-new'
    sessionToken.value = 'token-new'
    await oldRequest

    expect(applied).toEqual([])
    expect(chat.chatMessages.value).toEqual([])
    await stream.sendStreamRequest('new', true)
    expect(applied.map((event) => event.run_id)).toEqual(['run-new'])
  })

  it('does not let a stale agent-response handle overwrite a new chat generation', () => {
    const chat = useAgentChat()
    const staleHandle = chat.beginAgentResponse(true)
    chat.resetChat()
    const currentHandle = chat.beginAgentResponse(true)

    chat.updateAgentResponse(staleHandle, '旧打字机输出')
    chat.updateAgentResponse(currentHandle, '新运行输出')

    expect(chat.chatMessages.value).toEqual([{ role: 'agent', text: '新运行输出' }])
  })
})
