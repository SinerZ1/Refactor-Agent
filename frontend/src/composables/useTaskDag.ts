import { shallowRef } from 'vue'
import type { Edge, Node } from '@vue-flow/core'
import { isRefactorPlan, type AgentEvent, type RefactorPlan } from '../types/agentEvents'

export type TaskStatus = 'pending' | 'in_progress' | 'completed' | 'failed'
const CLOSED_ARROW_MARKER = 'arrowclosed'
const TASK_ID_PATTERN = /^[a-z][a-z0-9_-]{0,63}$/

const initialNodes: Node[] = [
  {
    id: 'architect_task',
    label: '📐 架构分析与重构规划',
    position: { x: 50, y: 180 },
    class: 'dag-node-pending',
    data: { status: 'pending', title: 'Architect Task' },
  },
  {
    id: 'models_py',
    label: '📦 重构 models.py (数据模型)',
    position: { x: 320, y: 50 },
    class: 'dag-node-pending',
    data: { status: 'pending', file: 'models.py' },
  },
  {
    id: 'calculator_py',
    label: '🧮 重构 Calculator.py (业务计算)',
    position: { x: 320, y: 180 },
    class: 'dag-node-pending',
    data: { status: 'pending', file: 'Calculator.py' },
  },
  {
    id: 'services_py',
    label: '🛠️ 重构 services.py (系统服务)',
    position: { x: 320, y: 310 },
    class: 'dag-node-pending',
    data: { status: 'pending', file: 'services.py' },
  },
  {
    id: 'main_py',
    label: '🚀 重构 main.py (入口编排)',
    position: { x: 590, y: 180 },
    class: 'dag-node-pending',
    data: { status: 'pending', file: 'main.py' },
  },
  {
    id: 'reviewer_task',
    label: '🛡️ Reviewer 自动化单元测试',
    position: { x: 860, y: 180 },
    class: 'dag-node-pending',
    data: { status: 'pending', title: 'Reviewer Task' },
  },
]

const initialEdges: Edge[] = [
  ['e1', 'architect_task', 'models_py'],
  ['e2', 'architect_task', 'calculator_py'],
  ['e3', 'architect_task', 'services_py'],
  ['e4', 'models_py', 'main_py'],
  ['e5', 'calculator_py', 'main_py'],
  ['e6', 'services_py', 'main_py'],
  ['e7', 'main_py', 'reviewer_task'],
].map(([id, source, target]) => ({
  id: id!,
  source: source!,
  target: target!,
  animated: false,
  style: { stroke: '#444' },
  markerEnd: CLOSED_ARROW_MARKER,
}))

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
  // 每个工作区实例复制初始值，避免 Vue Flow 对模块级模板对象的可变更新跨实例泄漏。
  const dagNodes = shallowRef<Node[]>(structuredClone(initialNodes))
  const dagEdges = shallowRef<Edge[]>(structuredClone(initialEdges))

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
      return {
        ...edge,
        animated: false,
        style: { stroke: '#444', strokeWidth: '1.5px' },
        markerEnd: CLOSED_ARROW_MARKER,
      }
    })
  }

  const findFileTaskId = (event: AgentEvent): string | undefined => {
    const args =
      event.payload && typeof event.payload.args === 'object' && event.payload.args !== null
        ? (event.payload.args as Record<string, unknown>)
        : undefined
    const rawPath = event.payload?.file_path ?? args?.file_path
    if (typeof rawPath !== 'string') return undefined
    const normalizedPath = normalizeTaskPath(rawPath)
    const exactMatch = dagNodes.value.find(
      (node) => normalizeTaskPath(String(node.data?.file ?? '')) === normalizedPath,
    )
    if (exactMatch) return exactMatch.id

    // 旧静态图只保存文件名。兼容期允许在没有完整路径命中时按唯一 basename 回退，
    // 动态计划始终使用完整 CodeSmells 相对路径，不会依赖这个分支。
    const fileName = normalizedPath.split('/').pop()
    const basenameMatches = dagNodes.value.filter(
      (node) =>
        normalizeTaskPath(String(node.data?.file ?? ''))
          .split('/')
          .pop() === fileName,
    )
    return basenameMatches.length === 1 ? basenameMatches[0]?.id : undefined
  }

  /**
   * 事件归约器是 DAG 的唯一写入口。它消费后端明确声明的事件语义，
   * 不再从中文日志、文件名片段或 Reviewer 自然语言中猜测状态。
   */
  const applyDagEvent = (event: AgentEvent) => {
    const explicitTaskId = event.task_id
    const fileTaskId = explicitTaskId ?? findFileTaskId(event)

    switch (event.type) {
      case 'plan.created': {
        const plan = event.payload?.plan
        if (isRefactorPlan(plan)) initializeDagFromPlan(plan)
        break
      }
      case 'task.started':
        if (explicitTaskId) updateDagNodeStatus(explicitTaskId, 'in_progress')
        break
      case 'task.completed':
        if (explicitTaskId) updateDagNodeStatus(explicitTaskId, 'completed')
        break
      case 'tool.started':
        if (event.tool === 'write_code_file' && fileTaskId) {
          updateDagNodeStatus(fileTaskId, 'in_progress')
        } else if (event.tool === 'run_unit_tests') {
          updateDagNodeStatus('reviewer_task', 'in_progress')
        }
        break
      case 'tool.completed':
        if (event.tool === 'write_code_file' && fileTaskId) {
          updateDagNodeStatus(fileTaskId, 'completed')
        }
        break
      case 'tool.failed':
      case 'approval.rejected':
        if (event.tool === 'write_code_file' && fileTaskId) {
          updateDagNodeStatus(fileTaskId, 'failed')
        } else if (event.tool === 'run_unit_tests') {
          updateDagNodeStatus('reviewer_task', 'failed')
        }
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
    }
  }

  const resetDag = () => {
    // 新会话必须丢弃上一轮动态计划，避免旧 DAG 在 Architect 生成新计划前短暂误导用户。
    dagNodes.value = structuredClone(initialNodes)
    dagEdges.value = structuredClone(initialEdges)
    updateDagNodeStatus('architect_task', 'in_progress')
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
