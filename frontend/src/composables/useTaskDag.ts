import { shallowRef } from 'vue'
import { MarkerType } from '@vue-flow/core'
import type { Edge, Node } from '@vue-flow/core'

export type TaskStatus = 'pending' | 'in_progress' | 'completed' | 'failed'

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
  markerEnd: MarkerType.ArrowClosed,
}))

export function useTaskDag() {
  // 每个工作区实例复制初始值，避免 Vue Flow 对模块级模板对象的可变更新跨实例泄漏。
  const dagNodes = shallowRef<Node[]>(structuredClone(initialNodes))
  const dagEdges = shallowRef<Edge[]>(structuredClone(initialEdges))

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
          markerEnd: MarkerType.ArrowClosed,
        }
      }
      if (status === 'failed') {
        return {
          ...edge,
          animated: false,
          style: { stroke: '#f44336', strokeWidth: '2px' },
          markerEnd: MarkerType.ArrowClosed,
        }
      }
      return edge
    })
  }

  const resetDag = () => {
    dagNodes.value = dagNodes.value.map((node) => ({
      ...node,
      class: 'dag-node-pending',
      data: { ...node.data, status: 'pending' },
    }))
    dagEdges.value = dagEdges.value.map((edge) => ({
      ...edge,
      animated: false,
      style: { stroke: '#444', strokeWidth: '1.5px' },
      markerEnd: MarkerType.ArrowClosed,
    }))
    updateDagNodeStatus('architect_task', 'in_progress')
  }

  return {
    dagEdges,
    dagNodes,
    resetDag,
    updateDagNodeStatus,
  }
}
