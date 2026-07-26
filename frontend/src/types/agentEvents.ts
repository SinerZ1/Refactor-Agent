export type AgentEventType =
  | 'run.started'
  | 'run.completed'
  | 'run.failed'
  | 'run.retrying'
  | 'plan.created'
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

export interface RefactorTask {
  id: string
  title: string
  description: string
  file_path: string
  dependencies: string[]
}

export interface RefactorPlan {
  version: 1
  summary: string
  tasks: RefactorTask[]
}

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
  'plan.created',
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

export const isRefactorPlan = (value: unknown): value is RefactorPlan => {
  if (!isRecord(value) || value.version !== 1 || typeof value.summary !== 'string') return false
  if (!Array.isArray(value.tasks) || value.tasks.length === 0 || value.tasks.length > 20)
    return false
  return value.tasks.every(
    (task) =>
      isRecord(task) &&
      typeof task.id === 'string' &&
      typeof task.title === 'string' &&
      typeof task.description === 'string' &&
      typeof task.file_path === 'string' &&
      Array.isArray(task.dependencies) &&
      task.dependencies.every((dependency) => typeof dependency === 'string'),
  )
}

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
  if (value.type === 'plan.created') {
    if (!isRecord(value.payload) || !isRefactorPlan(value.payload.plan)) return null
  }
  return value as unknown as AgentEvent
}
