import { ref, type Ref } from 'vue'

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
  const socket = ref<WebSocket | null>(null)
  let messageHandler: MessageHandler = () => undefined

  const createBackendSession = async () => {
    const response = await fetch(`${API_BASE_URL}/api/sessions`, { method: 'POST' })
    if (!response.ok) throw new Error(`创建后端会话失败（HTTP ${response.status}）`)

    const payload = (await response.json()) as Record<string, unknown>
    if (typeof payload.thread_id !== 'string' || typeof payload.session_token !== 'string') {
      throw new Error('创建后端会话失败：响应缺少会话凭据')
    }
    threadId.value = payload.thread_id
    sessionToken.value = payload.session_token
  }

  const closeWebSocket = () => {
    socket.value?.close()
    socket.value = null
  }

  const connectWebSocket = () => {
    if (!threadId.value || !sessionToken.value) return
    closeWebSocket()

    const apiUrl = new URL(API_BASE_URL)
    const protocol = apiUrl.protocol === 'https:' ? 'wss:' : 'ws:'
    const url = `${protocol}//${apiUrl.host}/ws/refactor/${encodeURIComponent(threadId.value)}?token=${encodeURIComponent(sessionToken.value)}`
    const nextSocket = new WebSocket(url)
    socket.value = nextSocket

    nextSocket.onmessage = (event) => {
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

  const startBackendSession = async () => {
    await createBackendSession()
    connectWebSocket()
  }

  const ensureBackendSession = async () => {
    if (!threadId.value || !sessionToken.value) await startBackendSession()
  }

  const resetBackendSession = async () => {
    closeWebSocket()
    threadId.value = ''
    sessionToken.value = ''
    await startBackendSession()
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
