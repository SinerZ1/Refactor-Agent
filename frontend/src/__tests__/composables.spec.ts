import { beforeEach, describe, expect, it } from 'vitest'

import { useModelProvider } from '../composables/useModelProvider'
import { useTaskDag } from '../composables/useTaskDag'
import { parseAgentEvent, type RefactorPlan } from '../types/agentEvents'

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

  it('builds a dynamic DAG from the validated Architect plan', () => {
    const { applyDagEvent, dagEdges, dagNodes, resetDag } = useTaskDag()
    const plan: RefactorPlan = {
      version: 1,
      summary: '先整理模型，再改造入口',
      tasks: [
        {
          id: 'domain_models',
          title: '整理领域模型',
          description: '拆分数据对象',
          file_path: 'CodeSmells/models.py',
          dependencies: [],
        },
        {
          id: 'entrypoint',
          title: '调整入口编排',
          description: '接入新的数据对象',
          file_path: 'CodeSmells/main.py',
          dependencies: ['domain_models'],
        },
      ],
    }
    const planEvent = parseAgentEvent({
      version: 1,
      type: 'plan.created',
      level: 'success',
      message: '计划已经生成',
      payload: { plan },
    })

    expect(planEvent).not.toBeNull()
    applyDagEvent(planEvent!)
    expect(dagNodes.value.map((node) => node.id)).toEqual([
      'architect_task',
      'domain_models',
      'entrypoint',
      'reviewer_task',
    ])
    expect(dagNodes.value[0]?.data.status).toBe('completed')
    expect(
      dagEdges.value.some(
        (edge) => edge.source === 'domain_models' && edge.target === 'entrypoint',
      ),
    ).toBe(true)

    applyDagEvent({
      version: 1,
      type: 'tool.started',
      level: 'info',
      message: '执行写入',
      tool: 'write_code_file',
      task_id: 'entrypoint',
    })
    expect(dagNodes.value.find((node) => node.id === 'entrypoint')?.data.status).toBe('in_progress')

    resetDag()
    expect(dagNodes.value.some((node) => node.id === 'domain_models')).toBe(false)
    expect(dagNodes.value.find((node) => node.id === 'architect_task')?.data.status).toBe(
      'in_progress',
    )
  })

  it('rejects cyclic plans before replacing the current DAG', () => {
    const { dagNodes, initializeDagFromPlan } = useTaskDag()
    const cyclicPlan: RefactorPlan = {
      version: 1,
      summary: '错误的循环依赖',
      tasks: [
        {
          id: 'first',
          title: '第一个任务',
          description: '测试',
          file_path: 'CodeSmells/first.py',
          dependencies: ['second'],
        },
        {
          id: 'second',
          title: '第二个任务',
          description: '测试',
          file_path: 'CodeSmells/second.py',
          dependencies: ['first'],
        },
      ],
    }

    expect(initializeDagFromPlan(cyclicPlan)).toBe(false)
    expect(dagNodes.value.some((node) => node.id === 'models_py')).toBe(true)
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
    expect(
      parseAgentEvent({
        version: 1,
        type: 'plan.created',
        level: 'success',
        message: 'missing plan payload',
        payload: {},
      }),
    ).toBeNull()
  })
})
