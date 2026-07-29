import { nextTick, ref, type Ref } from 'vue'

import { API_BASE_URL } from '../api'
import { parseAgentEvent, type AgentEvent } from '../types/agentEvents'
import type { AgentLog } from '../types/workspace'

interface RefactorStreamOptions {
  activeRunId: Ref<string>
  threadId: Ref<string>
  sessionToken: Ref<string>
  ensureBackendSession: () => Promise<void>
  getRuntimeModelConfig: () => Record<string, unknown>
  beginAgentResponse: (isInitialTurn: boolean) => number
  updateAgentResponse: (index: number, text: string) => void
  resetInitialTurn: () => void
  applyEvent: (event: AgentEvent) => void
  onApproval: (payload: unknown) => void
  onLog: (log: AgentLog) => void
  onPlanCreated: () => void
  onStreamSettled: () => void
}

const now = () => new Date().toLocaleTimeString()

export function useRefactorStream(options: RefactorStreamOptions) {
  const isRefactoring = ref(false)
  const refactoredCode = ref('')
  let abortController: AbortController | null = null
  let streamGeneration = 0

  const cancelActiveStream = () => {
    streamGeneration += 1
    abortController?.abort()
    abortController = null
    isRefactoring.value = false
  }

  const sendStreamRequest = async (payloadText: string, isInitialTurn: boolean) => {
    try {
      await options.ensureBackendSession()
    } catch (error) {
      options.onLog({
        type: 'error',
        message: error instanceof Error ? error.message : '无法创建后端会话',
        time: now(),
      })
      return
    }

    abortController?.abort()
    const controller = new AbortController()
    abortController = controller
    const generation = ++streamGeneration
    const requestThreadId = options.threadId.value
    const requestSessionToken = options.sessionToken.value
    isRefactoring.value = true

    if (isInitialTurn) {
      refactoredCode.value = ''
      options.resetInitialTurn()
    }
    const agentMessageIndex = options.beginAgentResponse(isInitialTurn)
    let accumulatedResponse = ''

    try {
      const response = await fetch(`${API_BASE_URL}/api/refactor/stream`, {
        method: 'POST',
        signal: controller.signal,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          code: payloadText,
          thread_id: requestThreadId,
          session_token: requestSessionToken,
          model_config: options.getRuntimeModelConfig(),
        }),
      })
      if (!response.ok) throw new Error(`HTTP error! status: ${response.status}`)

      const reader = response.body?.getReader()
      if (!reader) throw new Error('未获取到 Stream Reader')
      const decoder = new TextDecoder()
      let buffer = ''

      while (true) {
        const { value, done } = await reader.read()
        if (generation !== streamGeneration) {
          await reader.cancel()
          return
        }
        if (done) break

        buffer += decoder.decode(value, { stream: true })
        const chunks = buffer.split('\n\n')
        buffer = chunks.pop() || ''
        for (const chunk of chunks) {
          if (!chunk.trim().startsWith('data: ')) continue
          try {
            const event = parseAgentEvent(JSON.parse(chunk.replace(/^data:\s*/, '')))
            if (!event) continue
            if (event.type === 'run.started' && event.run_id) {
              options.activeRunId.value = event.run_id
            } else if (
              event.run_id &&
              options.activeRunId.value &&
              event.run_id !== options.activeRunId.value
            ) {
              // run_id 是 SSE 与 WebSocket 共用的 happens-before 边界，晚到的旧运行
              // 不能覆盖当前工作区。AbortController 只负责传输取消，不能替代协议过滤。
              continue
            }

            options.applyEvent(event)
            if (event.type === 'plan.created') options.onPlanCreated()
            if (event.type === 'agent.message.delta') {
              accumulatedResponse += event.message
              refactoredCode.value = accumulatedResponse
              options.updateAgentResponse(agentMessageIndex, accumulatedResponse)
            } else if (event.type === 'approval.waiting') {
              options.onApproval(event.payload)
            } else {
              options.onLog({ type: event.level, message: event.message, time: now() })
            }
          } catch (error) {
            console.error('解析 SSE 失败:', chunk, error)
          }
        }
      }
    } catch (error) {
      if (
        generation !== streamGeneration ||
        controller.signal.aborted ||
        (error instanceof DOMException && error.name === 'AbortError')
      ) {
        return
      }
      console.error('SSE Error:', error)
      options.onLog({ type: 'error', message: `错误: ${error}`, time: now() })
      options.updateAgentResponse(
        agentMessageIndex,
        '[重构失败] 无法完成此次对话，请检查后端运行状态。',
      )
    } finally {
      if (generation === streamGeneration) {
        abortController = null
        isRefactoring.value = false
        nextTick(options.onStreamSettled)
      }
    }
  }

  return {
    cancelActiveStream,
    isRefactoring,
    refactoredCode,
    sendStreamRequest,
  }
}
