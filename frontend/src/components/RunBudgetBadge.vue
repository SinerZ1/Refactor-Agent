<script setup lang="ts">
import type { RunBudgetLimits, RunUsage } from '../types/agentEvents'

defineProps<{
  exceededReason: string
  isBudgetExceeded: boolean
  limits: RunBudgetLimits | null
  usage: RunUsage | null
}>()
</script>

<template>
  <span
    v-if="usage && limits"
    class="badge-neo4j budget-badge"
    :class="{ exceeded: isBudgetExceeded }"
    :title="isBudgetExceeded ? exceededReason : `未计量模型回合：${usage.unmetered_steps}`"
  >
    预算 {{ usage.agent_steps }}/{{ limits.max_agent_steps }} 步 · {{ usage.tool_calls }}/{{
      limits.max_tool_calls
    }}
    工具 · {{ usage.total_tokens.toLocaleString() }}/{{ limits.max_total_tokens.toLocaleString() }}
    Token
  </span>
</template>

<style scoped>
.budget-badge {
  margin-left: 10px;
  background-color: #1e3a5f;
}

.budget-badge.exceeded {
  background-color: #7f1d1d;
}
</style>
