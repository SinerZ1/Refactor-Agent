<script setup lang="ts">
import { nextTick, ref, watch } from 'vue'

import type { AgentLog } from '../types/workspace'

const props = defineProps<{ logs: AgentLog[] }>()
const containerRef = ref<HTMLDivElement | null>(null)

watch(
  () => props.logs,
  () =>
    nextTick(() => {
      if (containerRef.value) containerRef.value.scrollTop = containerRef.value.scrollHeight
    }),
  { deep: true },
)
</script>

<template>
  <div class="panel display-full run-log-panel">
    <div class="panel-header">
      <h3><span class="panel-icon" aria-hidden="true">⌘</span> Agent 思考与工具调用日志</h3>
    </div>
    <div ref="containerRef" class="log-content">
      <div v-if="logs.length === 0" class="empty-logs">等待 Agent 执行操作...</div>
      <div v-for="(log, index) in logs" :key="index" :class="['log-item', log.type]">
        <span class="log-time">[{{ log.time }}]</span>
        <pre class="log-message">{{ log.message }}</pre>
      </div>
    </div>
  </div>
</template>

<style scoped>
.run-log-panel {
  flex: 1;
  height: 100%;
}
</style>
