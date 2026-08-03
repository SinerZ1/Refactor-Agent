import { ref } from 'vue'

import type { BackendMessage, ChatMessage, ChatRole } from '../types/workspace'

export interface AgentResponseHandle {
  generation: number
  index: number
}

const roleForSender = (sender: unknown): ChatRole => {
  if (sender === 'ReviewerAgent') return 'reviewer'
  if (sender === 'ArchitectAgent') return 'architect'
  return 'coder'
}

export function useAgentChat() {
  const chatMessages = ref<ChatMessage[]>([])
  const userChatInput = ref('')
  let chatGeneration = 0

  const appendMessage = (role: ChatRole, text: string) => {
    chatMessages.value.push({ role, text })
  }

  const beginAgentResponse = (isInitialTurn: boolean) => {
    const index = chatMessages.value.length
    appendMessage(
      'agent',
      isInitialTurn ? '正在进行首次代码分析与重构...' : '正在思考...',
    )
    return { generation: chatGeneration, index }
  }

  const updateAgentResponse = (handle: AgentResponseHandle, text: string) => {
    if (handle.generation !== chatGeneration) return
    const message = chatMessages.value[handle.index]
    if (message) message.text = text
  }

  const takeUserMessage = () => {
    const text = userChatInput.value.trim()
    if (!text) return null
    appendMessage('user', text)
    userChatInput.value = ''
    return text
  }

  const handleBackendMessage = (message: BackendMessage) => {
    if (message.type !== 'chatroom_message' || typeof message.content !== 'string') return false
    appendMessage(roleForSender(message.sender), message.content)
    return true
  }

  const resetChat = () => {
    chatGeneration += 1
    userChatInput.value = ''
    chatMessages.value = []
  }

  return {
    beginAgentResponse,
    chatMessages,
    handleBackendMessage,
    resetChat,
    takeUserMessage,
    updateAgentResponse,
    userChatInput,
  }
}
