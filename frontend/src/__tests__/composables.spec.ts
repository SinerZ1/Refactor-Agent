import { beforeEach, describe, expect, it } from 'vitest'

import { useModelProvider } from '../composables/useModelProvider'
import { useTaskDag } from '../composables/useTaskDag'

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
})
