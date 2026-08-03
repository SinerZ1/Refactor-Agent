import { computed, ref, type Ref } from 'vue'

import { API_BASE_URL } from '../api'
import type {
  AgentEvent,
  RollbackStatus,
  RunLifecycleStatus,
  RunStatusSnapshot,
  WorkspaceApplyFailure,
} from '../types/agentEvents'

const LIFECYCLE_STATUSES = new Set<RunLifecycleStatus>([
  'running',
  'waiting_for_hitl',
  'cancelling',
  'cleanup_pending',
  'cleanup_completed',
  'cleanup_failed',
  'completed',
  'failed',
])
const ROLLBACK_STATUSES = new Set<RollbackStatus>(['not_started', 'complete', 'partial'])
const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null

export const parseWorkspaceApplyFailure = (value: unknown): WorkspaceApplyFailure | null => {
  if (!isRecord(value)) return null
  if (
    typeof value.code !== 'string' ||
    typeof value.phase !== 'string' ||
    typeof value.conflict_category !== 'string' ||
    typeof value.rollback_status !== 'string' ||
    !ROLLBACK_STATUSES.has(value.rollback_status as RollbackStatus) ||
    typeof value.requires_manual_action !== 'boolean' ||
    typeof value.affected_file_count !== 'number' ||
    typeof value.recovery_available !== 'boolean' ||
    typeof value.guidance !== 'string' ||
    !Array.isArray(value.affected_files) ||
    !value.affected_files.every(
      (path) => typeof path === 'string' && /^CodeSmells\//i.test(path) && !path.includes('..'),
    ) ||
    !Array.isArray(value.recovery_ids) ||
    !value.recovery_ids.every((item) => typeof item === 'string')
  ) {
    return null
  }
  return value as unknown as WorkspaceApplyFailure
}

export const parseRunStatusSnapshot = (value: unknown): RunStatusSnapshot | null => {
  if (!isRecord(value)) return null
  if (
    value.version !== 1 ||
    typeof value.run_id !== 'string' ||
    typeof value.lifecycle_status !== 'string' ||
    !LIFECYCLE_STATUSES.has(value.lifecycle_status as RunLifecycleStatus) ||
    ![null, 'completed', 'failed'].includes(value.terminal_status as string | null) ||
    (value.termination_reason !== null && typeof value.termination_reason !== 'string') ||
    typeof value.workspace_retained !== 'boolean' ||
    !Array.isArray(value.cleanup_errors) ||
    !value.cleanup_errors.every((item) => typeof item === 'string')
  ) {
    return null
  }
  const applyFailure =
    value.apply_failure === null ? null : parseWorkspaceApplyFailure(value.apply_failure)
  if (value.apply_failure !== null && applyFailure === null) return null
  return { ...(value as unknown as RunStatusSnapshot), apply_failure: applyFailure }
}

interface RunLifecycleOptions {
  activeRunId: Ref<string>
  threadId: Ref<string>
  sessionToken: Ref<string>
  onError: (message: string) => void
}

export function useRunLifecycle(options: RunLifecycleOptions) {
  const status = ref<RunStatusSnapshot | null>(null)
  let refreshGeneration = 0

  const ensureBase = (event: AgentEvent, lifecycleStatus: RunLifecycleStatus) => {
    const runId = event.run_id || options.activeRunId.value
    if (!runId) return
    status.value = {
      version: 1,
      run_id: runId,
      lifecycle_status: lifecycleStatus,
      terminal_status:
        lifecycleStatus === 'completed'
          ? 'completed'
          : lifecycleStatus === 'failed'
            ? 'failed'
            : null,
      termination_reason: null,
      workspace_retained: lifecycleStatus === 'waiting_for_hitl',
      cleanup_errors: [],
      apply_failure: status.value?.run_id === runId ? status.value.apply_failure : null,
    }
  }

  const applyEvent = (event: AgentEvent) => {
    if (event.type === 'run.started') ensureBase(event, 'running')
    else if (event.type === 'approval.waiting') ensureBase(event, 'waiting_for_hitl')
    else if (event.type === 'run.completed') ensureBase(event, 'completed')
    else if (event.type === 'run.failed') ensureBase(event, 'failed')

    if (event.type === 'run.lifecycle.updated') {
      const snapshot = parseRunStatusSnapshot(event.payload?.snapshot)
      if (snapshot) status.value = snapshot
    }
    if (event.type === 'workspace.apply.failed' || event.type === 'run.failed') {
      const failure = parseWorkspaceApplyFailure(event.payload?.apply_failure)
      if (failure && status.value) status.value = { ...status.value, apply_failure: failure }
    }
  }

  const refresh = async () => {
    const generation = ++refreshGeneration
    const runId = options.activeRunId.value
    const requestThread = options.threadId.value
    const token = options.sessionToken.value
    if (!runId || !requestThread || !token) return
    let response: Response
    try {
      response = await fetch(
        `${API_BASE_URL}/api/sessions/${encodeURIComponent(requestThread)}/runs/${encodeURIComponent(runId)}`,
        { headers: { 'X-Session-Token': token } },
      )
    } catch {
      if (generation === refreshGeneration) options.onError('无法读取运行清理状态（网络错误）')
      return
    }
    if (generation !== refreshGeneration || response.status === 404) return
    if (!response.ok) {
      options.onError(`无法读取运行清理状态（HTTP ${response.status}）`)
      return
    }
    let payload: unknown
    try {
      payload = await response.json()
    } catch {
      if (generation === refreshGeneration) options.onError('运行清理状态响应格式无效')
      return
    }
    const snapshot = parseRunStatusSnapshot(payload)
    if (generation === refreshGeneration && snapshot?.run_id === options.activeRunId.value) {
      status.value = snapshot
    }
  }

  const reset = () => {
    refreshGeneration += 1
    status.value = null
  }

  return {
    applyEvent,
    applyFailure: computed(() => status.value?.apply_failure ?? null),
    refresh,
    reset,
    status,
  }
}
