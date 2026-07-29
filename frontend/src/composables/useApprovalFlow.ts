import { ref, type Ref } from 'vue'

import { API_BASE_URL } from '../api'
import type { ApprovalPayload, BackendMessage } from '../types/workspace'

interface ApprovalFlowOptions {
  threadId: Ref<string>
  sessionToken: Ref<string>
  socket: Ref<WebSocket | null>
  onResume: () => Promise<void>
  onError: (message: string) => void
}

export const parseApprovalPayload = (value: unknown): ApprovalPayload | null => {
  if (!value || typeof value !== 'object') return null
  const payload = value as Record<string, unknown>
  if (
    typeof payload.approval_id !== 'string' ||
    typeof payload.file_path !== 'string' ||
    typeof payload.original_code !== 'string' ||
    typeof payload.refactored_code !== 'string'
  ) {
    return null
  }
  return payload as unknown as ApprovalPayload
}

export function useApprovalFlow({
  threadId,
  sessionToken,
  socket,
  onResume,
  onError,
}: ApprovalFlowOptions) {
  const approvalPayload = ref<ApprovalPayload | null>(null)
  const isApprovalModalOpen = ref(false)

  const openApproval = (value: unknown, invalidMessage: string) => {
    const payload = parseApprovalPayload(value)
    if (!payload) {
      onError(invalidMessage)
      return false
    }
    approvalPayload.value = payload
    isApprovalModalOpen.value = true
    return true
  }

  const handleBackendMessage = (message: BackendMessage) => {
    if (message.type === 'approval_request') {
      openApproval(message.payload, '收到 WebSocket 审批请求但数据缺失')
      return true
    }
    if (message.type === 'approval_confirmed') {
      isApprovalModalOpen.value = false
      void onResume()
      return true
    }
    if (message.type === 'approval_error') {
      onError(typeof message.message === 'string' ? message.message : '审批请求已失效')
      return true
    }
    return false
  }

  const handleStreamApproval = (value: unknown) =>
    openApproval(value, '收到的审批事件缺少必要字段')

  const submitApprovalDecision = async (approved: boolean) => {
    if (!approvalPayload.value) return
    if (socket.value?.readyState === WebSocket.OPEN) {
      socket.value.send(
        JSON.stringify({
          type: 'approval_response',
          approval_id: approvalPayload.value.approval_id,
          approved,
        }),
      )
      return
    }

    const response = await fetch(
      `${API_BASE_URL}/api/sessions/${encodeURIComponent(threadId.value)}/approval`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          session_token: sessionToken.value,
          approval_id: approvalPayload.value.approval_id,
          approved,
        }),
      },
    )
    if (!response.ok) {
      const payload = (await response.json().catch(() => ({}))) as { detail?: string }
      throw new Error(payload.detail || `审批提交失败（HTTP ${response.status}）`)
    }
    isApprovalModalOpen.value = false
    await onResume()
  }

  const decideApproval = async (approved: boolean) => {
    try {
      await submitApprovalDecision(approved)
    } catch (error) {
      onError(error instanceof Error ? error.message : '审批提交失败')
    }
  }

  const resetApproval = () => {
    approvalPayload.value = null
    isApprovalModalOpen.value = false
  }

  return {
    approvalPayload,
    approve: () => decideApproval(true),
    handleBackendMessage,
    handleStreamApproval,
    isApprovalModalOpen,
    reject: () => decideApproval(false),
    resetApproval,
    submitApprovalDecision,
  }
}
