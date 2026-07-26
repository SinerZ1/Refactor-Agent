import { beforeEach, describe, expect, it } from 'vitest'

import { useModelProvider } from '../composables/useModelProvider'
import { useTaskDag } from '../composables/useTaskDag'
import { parseAgentEvent } from '../types/agentEvents'

describe('workspace composables', () => {
  beforeEach(() => {
    localStorage.clear()
  })

  it('keeps legacy secrets out of the restored model configuration', () => {
    localStorage.setItem(
      'refactor_agent_model_config',
      JSON.stringify({
        provider: 'openai',
        model_name: 'saved-model',
        api_key: 'must-not-survive',
        vertex_adc_path: 'C:/credentials.json',
      }),
    )

    const { getRuntimeModelConfig, modelConfig } = useModelProvider()

    expect(modelConfig.value.model_name).toBe('saved-model')
    expect(getRuntimeModelConfig().api_key).toBe('')
    expect(getRuntimeModelConfig()).not.toHaveProperty('vertex_adc_path')
  })

  it('updates and resets task DAG state through immutable snapshots', () => {
    const { dagEdges, dagNodes, resetDag, updateDagNodeStatus } = useTaskDag()

    updateDagNodeStatus('models_py', 'completed')
    expect(dagNodes.value.find((node) => node.id === 'models_py')?.data.status).toBe('completed')
    expect(dagEdges.value.find((edge) => edge.source === 'models_py')?.animated).toBe(true)

    resetDag()
    expect(dagNodes.value.find((node) => node.id === 'architect_task')?.data.status).toBe(
      'in_progress',
    )
    expect(dagNodes.value.find((node) => node.id === 'models_py')?.data.status).toBe('pending')
    expect(dagEdges.value.every((edge) => edge.animated === false)).toBe(true)
  })

  it('reduces structured tool events without parsing log text', () => {
    const { applyDagEvent, dagNodes } = useTaskDag()
    const started = parseAgentEvent({
      version: 1,
      type: 'tool.started',
      level: 'info',
      message: '文案可以任意变化',
      tool: 'write_code_file',
      payload: { args: { file_path: 'CodeSmells/models.py' } },
    })
    const failed = parseAgentEvent({
      version: 1,
      type: 'tool.failed',
      level: 'error',
      message: '不依赖失败关键字',
      tool: 'write_code_file',
      success: false,
      payload: { file_path: 'CodeSmells/models.py' },
    })

    expect(started).not.toBeNull()
    expect(failed).not.toBeNull()
    applyDagEvent(started!)
    expect(dagNodes.value.find((node) => node.id === 'models_py')?.data.status).toBe('in_progress')
    applyDagEvent(failed!)
    expect(dagNodes.value.find((node) => node.id === 'models_py')?.data.status).toBe('failed')
  })

  it('rejects malformed event envelopes', () => {
    expect(
      parseAgentEvent({
        version: 2,
        type: 'tool.completed',
        level: 'success',
        message: 'unsupported version',
      }),
    ).toBeNull()
  })
})
