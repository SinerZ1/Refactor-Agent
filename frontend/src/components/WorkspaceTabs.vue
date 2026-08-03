<script setup lang="ts">
import { defineAsyncComponent, nextTick, ref, watch } from 'vue'
import type { Edge, Node } from '@vue-flow/core'
import '@vue-flow/core/dist/style.css'
import '@vue-flow/core/dist/theme-default.css'

import type { RunBudgetLimits, RunStatusSnapshot, RunUsage } from '../types/agentEvents'
import type { AgentLog } from '../types/workspace'
import AgentRunLog from './AgentRunLog.vue'
import RunBudgetBadge from './RunBudgetBadge.vue'
import RunLifecycleNotice from './RunLifecycleNotice.vue'

const VueFlow = defineAsyncComponent(() =>
  import('@vue-flow/core').then((module) => module.VueFlow),
)
const TopologyGraph = defineAsyncComponent(() => import('./TopologyGraph.vue'))

const nodes = defineModel<Node[]>('nodes', { required: true })
const edges = defineModel<Edge[]>('edges', { required: true })
defineProps<{
  budgetExceededReason: string
  budgetLimits: RunBudgetLimits | null
  budgetUsage: RunUsage | null
  isBudgetExceeded: boolean
  logs: AgentLog[]
  runStatus: RunStatusSnapshot | null
  theme: 'light' | 'dark'
}>()

const activeTab = ref<'code' | 'dag' | 'topology'>('code')
const topologyGraphRef = ref<InstanceType<typeof TopologyGraph> | null>(null)
const taskDagGraphRef = ref<{ fitView: (options?: { padding?: number }) => void } | null>(null)

const fitDag = () => nextTick(() => taskDagGraphRef.value?.fitView({ padding: 0.15 }))
const refreshTopology = () => nextTick(() => topologyGraphRef.value?.refresh())

watch(activeTab, (tab) => {
  if (tab === 'topology') nextTick(() => topologyGraphRef.value?.resize())
  if (tab === 'dag') fitDag()
})

defineExpose({
  handlePlanCreated: () => {
    if (activeTab.value === 'dag') fitDag()
  },
  refreshTopology,
})
</script>

<template>
  <div class="column col-display">
    <div class="tab-header">
      <button :class="['tab-btn', { active: activeTab === 'code' }]" @click="activeTab = 'code'">
        <span aria-hidden="true">⌘</span> 运行日志
      </button>
      <button :class="['tab-btn', { active: activeTab === 'dag' }]" @click="activeTab = 'dag'">
        <span aria-hidden="true">◇</span> 任务 DAG
      </button>
      <button
        :class="['tab-btn', { active: activeTab === 'topology' }]"
        @click="activeTab = 'topology'"
      >
        <span aria-hidden="true">⌘</span> 依赖图谱
      </button>
    </div>

    <div v-show="activeTab === 'code'" class="tab-content flex-column log-tab">
      <RunLifecycleNotice :status="runStatus" />
      <AgentRunLog :logs="logs" />
    </div>

    <div v-if="activeTab === 'dag'" class="tab-content graph-tab">
      <div class="panel graph-panel">
        <div class="topology-toolbar">
          <span class="title">
            <span class="panel-icon" aria-hidden="true">◇</span> 重构任务 DAG 进度看板
          </span>
          <span class="badge-neo4j orchestration-badge">任务编排</span>
          <RunBudgetBadge
            :exceeded-reason="budgetExceededReason"
            :is-budget-exceeded="isBudgetExceeded"
            :limits="budgetLimits"
            :usage="budgetUsage"
          />
        </div>
        <div class="graph-canvas">
          <VueFlow
            ref="taskDagGraphRef"
            v-model:nodes="nodes"
            v-model:edges="edges"
            :fit-view-on-init="true"
            :nodes-draggable="true"
            :zoom-on-scroll="true"
            :zoom-on-pinch="true"
            :zoom-on-double-click="false"
          />
        </div>
      </div>
    </div>

    <div v-if="activeTab === 'topology'" class="tab-content graph-tab">
      <div class="panel graph-panel">
        <TopologyGraph ref="topologyGraphRef" :theme="theme" />
      </div>
    </div>
  </div>
</template>

<style scoped>
.log-tab {
  gap: 0.8rem;
  height: calc(100% - 44px);
}

.graph-tab {
  height: calc(100% - 44px);
}

.graph-panel {
  height: 100%;
}

.graph-canvas {
  flex: 1;
  width: 100%;
  height: 100%;
  min-height: 350px;
}

.orchestration-badge {
  margin-left: 10px;
  background-color: #0b533e;
}
</style>
