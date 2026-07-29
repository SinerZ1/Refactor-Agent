export type LogLevel = 'info' | 'success' | 'error'

export interface AgentLog {
  type: LogLevel
  message: string
  time: string
}

export type ChatRole = 'user' | 'agent' | 'coder' | 'reviewer' | 'architect'

export interface ChatMessage {
  role: ChatRole
  text: string
}

export interface ApprovalPayload {
  approval_id: string
  file_path: string
  original_code: string
  refactored_code: string
}

export type BackendMessage = Record<string, unknown> & {
  type?: string
  run_id?: string
}
