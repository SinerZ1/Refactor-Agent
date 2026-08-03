import { describe, expect, it, vi } from 'vitest'
import { ref } from 'vue'
import { mount } from '@vue/test-utils'

import RunLifecycleNotice from '../components/RunLifecycleNotice.vue'
import {
  parseRunStatusSnapshot,
  parseWorkspaceApplyFailure,
  useRunLifecycle,
} from '../composables/useRunLifecycle'
import type { AgentEvent, RunStatusSnapshot } from '../types/agentEvents'

const snapshot = (overrides: Partial<RunStatusSnapshot> = {}): RunStatusSnapshot => ({
  version: 1,
  run_id: 'run-one',
  lifecycle_status: 'running',
  terminal_status: null,
  termination_reason: null,
  workspace_retained: false,
  cleanup_errors: [],
  apply_failure: null,
  ...overrides,
})

describe('run lifecycle observability', () => {
  it.each([
    ['cleanup_pending', '正在安全清理'],
    ['cleanup_completed', '延迟清理已完成'],
    ['cleanup_failed', '延迟清理失败'],
  ] as const)('renders %s as a distinct lifecycle state', (lifecycle, message) => {
    const wrapper = mount(RunLifecycleNotice, {
      props: { status: snapshot({ lifecycle_status: lifecycle }) },
    })
    expect(wrapper.text()).toContain(message)
    if (lifecycle === 'cleanup_pending') expect(wrapper.text()).not.toContain('清理已完成')
  })

  it('distinguishes complete and partial rollback guidance', async () => {
    const wrapper = mount(RunLifecycleNotice, {
      props: {
        status: snapshot({
          lifecycle_status: 'failed',
          terminal_status: 'failed',
          apply_failure: {
            code: 'atomic_apply_failed',
            phase: 'commit',
            conflict_category: 'atomic_apply_failed',
            rollback_status: 'complete',
            requires_manual_action: false,
            affected_file_count: 1,
            affected_files: ['CodeSmells/example.py'],
            recovery_available: false,
            recovery_ids: [],
            guidance: '已回滚',
          },
        }),
      },
    })
    expect(wrapper.text()).toContain('已完成补偿回滚')

    await wrapper.setProps({
      status: snapshot({
        lifecycle_status: 'failed',
        terminal_status: 'failed',
        apply_failure: {
          code: 'atomic_apply_failed',
          phase: 'rollback',
          conflict_category: 'atomic_apply_failed',
          rollback_status: 'partial',
          requires_manual_action: true,
          affected_file_count: 1,
          affected_files: ['CodeSmells/example.py'],
          recovery_available: true,
          recovery_ids: ['recovery-safe-id'],
          guidance: '人工核对',
        },
      }),
    })
    expect(wrapper.text()).toContain('未能完整恢复')
    expect(wrapper.text()).toContain('CodeSmells/example.py')
    expect(wrapper.text()).toContain('recovery-safe-id')
    expect(wrapper.html()).not.toContain('v-html')
  })

  it('rejects unsafe paths and never renders source payloads', () => {
    expect(
      parseWorkspaceApplyFailure({
        code: 'atomic_apply_failed',
        phase: 'rollback',
        conflict_category: 'atomic_apply_failed',
        rollback_status: 'partial',
        requires_manual_action: true,
        affected_file_count: 1,
        affected_files: ['C:/private/source.py'],
        recovery_available: true,
        recovery_ids: ['id'],
        guidance: 'manual',
        source: 'SECRET SOURCE',
      }),
    ).toBeNull()
  })

  it('ignores unknown fields while preserving the versioned snapshot', () => {
    const parsed = parseRunStatusSnapshot({ ...snapshot(), future_field: { nested: true } })
    expect(parsed?.run_id).toBe('run-one')
  })

  it('keeps legacy workspace_error run.failed events displayable', () => {
    const lifecycle = useRunLifecycle({
      activeRunId: ref('run-one'),
      threadId: ref('thread-one'),
      sessionToken: ref('token'),
      onError: vi.fn(),
    })
    lifecycle.applyEvent({
      version: 1,
      type: 'run.failed',
      level: 'error',
      message: '旧格式 workspace_error',
      run_id: 'run-one',
      payload: { workspace_error: '旧格式 workspace_error' },
    } satisfies AgentEvent)

    expect(lifecycle.status.value?.lifecycle_status).toBe('failed')
    expect(lifecycle.status.value?.apply_failure).toBeNull()
  })
})
