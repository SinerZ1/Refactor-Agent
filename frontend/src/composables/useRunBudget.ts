import { computed, ref } from 'vue'

import {
  isRunBudgetLimits,
  isRunUsage,
  type AgentEvent,
  type RunBudgetLimits,
  type RunUsage,
} from '../types/agentEvents'

export function useRunBudget() {
  const usage = ref<RunUsage | null>(null)
  const limits = ref<RunBudgetLimits | null>(null)
  const exceededReason = ref('')

  /**
   * 预算事件是后端 circuit breaker 的只读投影，前端不自行估算 Token 或判定超限。
   * 这避免不同 tokenizer 造成 UI 与执行引擎出现两个互相矛盾的“真相来源”。
   */
  const applyRunBudgetEvent = (event: AgentEvent) => {
    if (event.type !== 'run.usage.updated' && event.type !== 'run.budget.exceeded') return
    const nextUsage = event.payload?.usage
    const nextLimits = event.payload?.limits
    if (!isRunUsage(nextUsage) || !isRunBudgetLimits(nextLimits)) return

    usage.value = nextUsage
    limits.value = nextLimits
    if (event.type === 'run.budget.exceeded') {
      exceededReason.value =
        typeof event.payload?.reason === 'string' ? event.payload.reason : event.message
    }
  }

  const resetRunBudget = () => {
    usage.value = null
    limits.value = null
    exceededReason.value = ''
  }

  const isBudgetExceeded = computed(() => Boolean(exceededReason.value))

  return {
    applyRunBudgetEvent,
    exceededReason,
    isBudgetExceeded,
    limits,
    resetRunBudget,
    usage,
  }
}
