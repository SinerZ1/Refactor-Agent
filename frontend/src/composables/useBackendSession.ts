import { ref, shallowRef, type Ref } from 'vue'

import { API_BASE_URL } from '../api'
import type { BackendMessage } from '../types/workspace'

interface BackendSessionOptions {
  activeRunId: Ref<string>
  onError: (message: string) => void
}

type MessageHandler = (message: BackendMessage) => void

const isBackendMessage = (value: unknown): value is BackendMessage =>
  typeof value === 'object' && value !== null

export function useBackendSession({ activeRunId, onError }: BackendSessionOptions) {
  const threadId = ref('')
  const sessionToken = ref('')
  const socket = shallowRef<WebSocket | null>(null)
  let messageHandler: MessageHandler = () => undefined
  let sessionGeneration = 0
  let disposed = false

  const requestBackendSession = async () => {
    const response = await fetch(`${API_BASE_URL}/api/sessions`, { method: 'POST' })
    if (!response.ok) throw new Error(`创建后端会话失败（HTTP ${response.status}）`)

    const payload = (await response.json()) as Record<string, unknown>
    if (typeof payload.thread_id !== 'string' || typeof payload.session_token !== 'string') {
      throw new Error('创建后端会话失败：响应缺少会话凭据')
    }
    return { threadId: payload.thread_id, sessionToken: payload.session_token }
  }

  const detachWebSocket = () => {
    const current = socket.value
    socket.value = null
    if (!current) return
    current.onmessage = null
    current.onclose = null
    current.onerror = null
    current.close()
  }

  const connectForGeneration = (generation: number) => {
    if (!threadId.value || !sessionToken.value) return
    detachWebSocket()

    const apiUrl = new URL(API_BASE_URL)
    const protocol = apiUrl.protocol === 'https:' ? 'wss:' : 'ws:'
    const url = `${protocol}//${apiUrl.host}/ws/refactor/${encodeURIComponent(threadId.value)}?token=${encodeURIComponent(sessionToken.value)}`
    const nextSocket = new WebSocket(url)
    socket.value = nextSocket
    const boundThreadId = threadId.value
    const boundSessionToken = sessionToken.value

    nextSocket.onmessage = (event) => {
      if (
        disposed ||
        generation !== sessionGeneration ||
        socket.value !== nextSocket ||
        threadId.value !== boundThreadId ||
        sessionToken.value !== boundSessionToken
      ) {
        return
      }
      try {
        const message: unknown = JSON.parse(String(event.data))
        if (!isBackendMessage(message)) return

        const messageRunId = typeof message.run_id === 'string' ? message.run_id : ''
        if (messageRunId && activeRunId.value && messageRunId !== activeRunId.value) return
        if (messageRunId && !activeRunId.value) activeRunId.value = messageRunId
        messageHandler(message)
      } catch (error) {
        console.error('[WebSocket] Failed to parse message:', error)
      }
    }
    nextSocket.onclose = () => console.log('[WebSocket] Disconnected.')
    nextSocket.onerror = (error) => console.error('[WebSocket] Error:', error)
  }

  const connectWebSocket = () => {
    disposed = false
    const generation = ++sessionGeneration
    connectForGeneration(generation)
  }

  const startBackendSession = async () => {
    disposed = false
    const generation = ++sessionGeneration
    detachWebSocket()
    const credentials = await requestBackendSession()
    if (disposed || generation !== sessionGeneration) return
    threadId.value = credentials.threadId
    sessionToken.value = credentials.sessionToken
    connectForGeneration(generation)
  }

  const ensureBackendSession = async () => {
    if (!threadId.value || !sessionToken.value) await startBackendSession()
  }

  const resetBackendSession = async () => {
    sessionGeneration += 1
    detachWebSocket()
    threadId.value = ''
    sessionToken.value = ''
    await startBackendSession()
  }

  const closeWebSocket = () => {
    sessionGeneration += 1
    detachWebSocket()
  }

  const disposeBackendSession = () => {
    disposed = true
    sessionGeneration += 1
    detachWebSocket()
    threadId.value = ''
    sessionToken.value = ''
    messageHandler = () => undefined
  }

  const setMessageHandler = (handler: MessageHandler) => {
    messageHandler = handler
  }

  const reportSessionError = (error: unknown) => {
    onError(error instanceof Error ? error.message : '无法创建后端会话')
  }

  return {
    closeWebSocket,
    connectWebSocket,
    disposeBackendSession,
    ensureBackendSession,
    reportSessionError,
    resetBackendSession,
    sessionToken,
    setMessageHandler,
    socket,
    startBackendSession,
    threadId,
  }
}
