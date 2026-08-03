import { shallowRef } from 'vue'
import type { Edge, Node } from '@vue-flow/core'
import { isRefactorPlan, type AgentEvent, type RefactorPlan } from '../types/agentEvents'

export type TaskStatus = 'pending' | 'in_progress' | 'completed' | 'failed' | 'blocked'
type BackendTaskStatus = 'pending' | 'running' | 'completed' | 'failed' | 'blocked'
const CLOSED_ARROW_MARKER = 'arrowclosed'
const TASK_ID_PATTERN = /^[a-z][a-z0-9_-]{0,63}$/

const createEdge = (source: string, target: string): Edge => ({
  id: `plan-edge-${source}-${target}`,
  source,
  target,
  animated: false,
  style: { stroke: '#444', strokeWidth: '1.5px' },
  markerEnd: CLOSED_ARROW_MARKER,
})

const normalizeTaskPath = (value: string): string => {
  const normalized = value
    .replace(/\\/g, '/')
    .replace(/^\.?\//, '')
    .toLowerCase()
  const workspaceIndex = normalized.indexOf('codesmells/')
  return workspaceIndex >= 0 ? normalized.slice(workspaceIndex) : normalized
}

export function useTaskDag() {
  const dagNodes = shallowRef<Node[]>([])
  const dagEdges = shallowRef<Edge[]>([])

  /**
   * 计划布局采用拓扑层级而非任务数组顺序：根任务位于 Architect 右侧，每个任务的
   * x 坐标严格晚于所有依赖，Reviewer 位于所有汇点之后。这是 DAG 偏序关系的视觉
   * 投影；同层任务只调整 y 坐标，因此布局变化不会改变真实调度语义。
   */
  const initializeDagFromPlan = (plan: RefactorPlan): boolean => {
    if (plan.tasks.length === 0 || plan.tasks.length > 20) return false
    const taskIds = new Set(plan.tasks.map((task) => task.id))
    if (
      taskIds.size !== plan.tasks.length ||
      taskIds.has('architect_task') ||
      taskIds.has('reviewer_task')
    ) {
      return false
    }

    const paths = new Set<string>()
    const indegree = new Map<string, number>()
    const dependents = new Map<string, string[]>()
    const levels = new Map<string, number>()
    for (const task of plan.tasks) {
      const normalizedPath = normalizeTaskPath(task.file_path)
      if (
        !TASK_ID_PATTERN.test(task.id) ||
        !task.title ||
        !normalizedPath.startsWith('codesmells/') ||
        !normalizedPath.endsWith('.py') ||
        normalizedPath.split('/').includes('..') ||
        paths.has(normalizedPath) ||
        new Set(task.dependencies).size !== task.dependencies.length ||
        task.dependencies.some((dependency) => dependency === task.id || !taskIds.has(dependency))
      ) {
        return false
      }
      paths.add(normalizedPath)
      indegree.set(task.id, task.dependencies.length)
      dependents.set(task.id, [])
      levels.set(task.id, 1)
    }

    for (const task of plan.tasks) {
      for (const dependency of task.dependencies) {
        dependents.get(dependency)?.push(task.id)
      }
    }

    const ready = plan.tasks.filter((task) => task.dependencies.length === 0).map((task) => task.id)
    let visited = 0
    while (ready.length > 0) {
      const taskId = ready.shift()!
      visited += 1
      for (const dependent of dependents.get(taskId) ?? []) {
        levels.set(dependent, Math.max(levels.get(dependent) ?? 1, (levels.get(taskId) ?? 1) + 1))
        const nextDegree = (indegree.get(dependent) ?? 1) - 1
        indegree.set(dependent, nextDegree)
        if (nextDegree === 0) ready.push(dependent)
      }
    }
    if (visited !== plan.tasks.length) return false

    const tasksByLevel = new Map<number, typeof plan.tasks>()
    for (const task of plan.tasks) {
      const level = levels.get(task.id) ?? 1
      tasksByLevel.set(level, [...(tasksByLevel.get(level) ?? []), task])
    }
    const maxLevel = Math.max(...levels.values())
    const maxRows = Math.max(...[...tasksByLevel.values()].map((tasks) => tasks.length))
    const centerY = 80 + ((maxRows - 1) * 140) / 2

    const planNodes: Node[] = [
      {
        id: 'architect_task',
        label: '📐 架构分析与重构规划',
        position: { x: 50, y: centerY },
        class: 'dag-node-completed',
        data: { status: 'completed', title: 'Architect Task', summary: plan.summary },
      },
    ]
    for (const [level, tasks] of tasksByLevel) {
      tasks.forEach((task, index) => {
        planNodes.push({
          id: task.id,
          label: `🧩 ${task.title}`,
          position: { x: 50 + level * 270, y: 80 + index * 140 },
          class: 'dag-node-pending',
          data: {
            status: 'pending',
            file: task.file_path,
            description: task.description,
          },
        })
      })
    }
    planNodes.push({
      id: 'reviewer_task',
      label: '🛡️ Reviewer 自动化单元测试',
      position: { x: 50 + (maxLevel + 1) * 270, y: centerY },
      class: 'dag-node-pending',
      data: { status: 'pending', title: 'Reviewer Task' },
    })

    const planEdges: Edge[] = []
    for (const task of plan.tasks) {
      if (task.dependencies.length === 0) {
        planEdges.push(createEdge('architect_task', task.id))
      } else {
        task.dependencies.forEach((dependency) => planEdges.push(createEdge(dependency, task.id)))
      }
      if ((dependents.get(task.id) ?? []).length === 0) {
        planEdges.push(createEdge(task.id, 'reviewer_task'))
      }
    }

    dagNodes.value = planNodes
    dagEdges.value = planEdges
    return true
  }

  const updateDagNodeStatus = (nodeId: string, status: TaskStatus) => {
    if (!dagNodes.value.some((candidate) => candidate.id === nodeId)) return

    dagNodes.value = dagNodes.value.map((node) =>
      node.id === nodeId
        ? { ...node, class: `dag-node-${status}`, data: { ...node.data, status } }
        : node,
    )
    dagEdges.value = dagEdges.value.map((edge) => {
      if (edge.source !== nodeId) return edge
      if (status === 'completed') {
        return {
          ...edge,
          animated: true,
          style: { stroke: '#4fc08d', strokeWidth: '3px' },
          markerEnd: CLOSED_ARROW_MARKER,
        }
      }
      if (status === 'failed') {
        return {
          ...edge,
          animated: false,
          style: { stroke: '#f44336', strokeWidth: '2px' },
          markerEnd: CLOSED_ARROW_MARKER,
        }
      }
      if (status === 'blocked') {
        return {
          ...edge,
          animated: false,
          style: { stroke: '#9e9e9e', strokeWidth: '2px', strokeDasharray: '6 4' },
          markerEnd: CLOSED_ARROW_MARKER,
        }
      }
      return {
        ...edge,
        animated: false,
        style: { stroke: '#444', strokeWidth: '1.5px' },
        markerEnd: CLOSED_ARROW_MARKER,
      }
    })
  }

  const applyBackendTaskSnapshot = (event: AgentEvent): boolean => {
    const rawStatuses = event.payload?.task_statuses
    if (typeof rawStatuses !== 'object' || rawStatuses === null || Array.isArray(rawStatuses)) {
      return false
    }
    const allowed = new Set<BackendTaskStatus>([
      'pending',
      'running',
      'completed',
      'failed',
      'blocked',
    ])
    for (const [taskId, rawStatus] of Object.entries(rawStatuses)) {
      if (typeof rawStatus !== 'string' || !allowed.has(rawStatus as BackendTaskStatus)) continue
      updateDagNodeStatus(
        taskId,
        rawStatus === 'running' ? 'in_progress' : (rawStatus as TaskStatus),
      )
    }
    return true
  }

  /**
   * 事件归约器是 DAG 的唯一写入口。它消费后端明确声明的事件语义，
   * 不再从中文日志、文件名片段或 Reviewer 自然语言中猜测状态。
   */
  const applyDagEvent = (event: AgentEvent) => {
    const explicitTaskId = event.task_id

    switch (event.type) {
      case 'plan.created': {
        const plan = event.payload?.plan
        if (isRefactorPlan(plan)) {
          initializeDagFromPlan(plan)
          applyBackendTaskSnapshot(event)
        }
        break
      }
      case 'task.started':
        if (!applyBackendTaskSnapshot(event) && explicitTaskId) {
          updateDagNodeStatus(explicitTaskId, 'in_progress')
        }
        break
      case 'task.completed':
        if (!applyBackendTaskSnapshot(event) && explicitTaskId) {
          updateDagNodeStatus(explicitTaskId, 'completed')
        }
        break
      case 'task.failed':
        if (!applyBackendTaskSnapshot(event) && explicitTaskId) {
          updateDagNodeStatus(explicitTaskId, 'failed')
        }
        break
      case 'task.blocked':
        if (!applyBackendTaskSnapshot(event) && explicitTaskId) {
          updateDagNodeStatus(explicitTaskId, 'blocked')
        }
        break
      case 'plan.completed':
      case 'plan.failed':
        applyBackendTaskSnapshot(event)
        break
      case 'review.passed':
      case 'run.completed':
        updateDagNodeStatus('reviewer_task', 'completed')
        break
      case 'review.failed':
      case 'run.failed':
        updateDagNodeStatus('reviewer_task', 'failed')
        break
      case 'run.retrying':
        updateDagNodeStatus('reviewer_task', 'in_progress')
        break
      case 'run.budget.exceeded':
        if (explicitTaskId) updateDagNodeStatus(explicitTaskId, 'failed')
        break
    }
  }

  const resetDag = () => {
    // 新会话必须丢弃上一轮动态计划，避免旧 DAG 在 Architect 生成新计划前短暂误导用户。
    dagNodes.value = []
    dagEdges.value = []
  }

  return {
    dagEdges,
    dagNodes,
    applyDagEvent,
    initializeDagFromPlan,
    resetDag,
    updateDagNodeStatus,
  }
}
