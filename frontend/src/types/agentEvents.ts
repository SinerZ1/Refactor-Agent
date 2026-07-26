export type AgentEventType =
  | 'run.started'
  | 'run.completed'
  | 'run.failed'
  | 'run.retrying'
  | 'task.started'
  | 'task.completed'
  | 'tool.started'
  | 'tool.completed'
  | 'tool.failed'
  | 'approval.waiting'
  | 'approval.rejected'
  | 'review.passed'
  | 'review.failed'
  | 'agent.message.delta'
  | 'log'

export type AgentEventLevel = 'info' | 'success' | 'error'

export interface AgentEvent {
  version: 1
  type: AgentEventType
  level: AgentEventLevel
  message: string
  token?: string
  node?: string
  task_id?: string
  tool?: string
  success?: boolean
  payload?: Record<string, unknown>
}

const EVENT_TYPES = new Set<AgentEventType>([
  'run.started',
  'run.completed',
  'run.failed',
  'run.retrying',
  'task.started',
  'task.completed',
  'tool.started',
  'tool.completed',
  'tool.failed',
  'approval.waiting',
  'approval.rejected',
  'review.passed',
  'review.failed',
  'agent.message.delta',
  'log',
])

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null

/**
 * SSE 是不可信的进程边界。这里做轻量运行时校验，避免畸形事件把 Vue 状态树
 * 污染成不可恢复状态；完整 schema 由后端版本号管理。
 */
export const parseAgentEvent = (value: unknown): AgentEvent | null => {
  if (!isRecord(value)) return null
  if (
    value.version !== 1 ||
    typeof value.type !== 'string' ||
    !EVENT_TYPES.has(value.type as AgentEventType) ||
    !['info', 'success', 'error'].includes(String(value.level)) ||
    typeof value.message !== 'string'
  ) {
    return null
  }
  return value as unknown as AgentEvent
}
