<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'

import AgentChatroom from './components/AgentChatroom.vue'
import AppHeader from './components/AppHeader.vue'
import ApprovalDiffModal from './components/ApprovalDiffModal.vue'
import ModelProviderPanel from './components/ModelProviderPanel.vue'
import SourceInputPanel from './components/SourceInputPanel.vue'
import WorkspaceTabs from './components/WorkspaceTabs.vue'
import { useAgentChat } from './composables/useAgentChat'
import { useApprovalFlow } from './composables/useApprovalFlow'
import { useBackendSession } from './composables/useBackendSession'
import { useRefactorStream } from './composables/useRefactorStream'
import { useRunBudget } from './composables/useRunBudget'
import { useRunLifecycle } from './composables/useRunLifecycle'
import { useTaskDag } from './composables/useTaskDag'
import { useTheme } from './composables/useTheme'
import type { AgentLog } from './types/workspace'

const sourceCode = ref('CodeSmells/main.py')
const activeRunId = ref('')
const agentLogs = ref<AgentLog[]>([])
const modelProviderRef = ref<InstanceType<typeof ModelProviderPanel> | null>(null)
const workspaceTabsRef = ref<InstanceType<typeof WorkspaceTabs> | null>(null)

const appendError = (message: string) => {
  agentLogs.value.push({ type: 'error', message, time: new Date().toLocaleTimeString() })
}

const { resolvedTheme, setThemePreference, themeOptions, themePreference } = useTheme()
const {
  beginAgentResponse,
  chatMessages,
  handleBackendMessage: handleChatMessage,
  resetChat,
  takeUserMessage,
  updateAgentResponse,
  userChatInput,
} = useAgentChat()
const { applyDagEvent, dagEdges, dagNodes, resetDag } = useTaskDag()
const {
  applyRunBudgetEvent,
  exceededReason,
  isBudgetExceeded,
  limits: budgetLimits,
  resetRunBudget,
  usage: budgetUsage,
} = useRunBudget()
const {
  disposeBackendSession,
  ensureBackendSession,
  reportSessionError,
  resetBackendSession,
  sessionToken,
  setMessageHandler,
  socket,
  startBackendSession,
  threadId,
} = useBackendSession({ activeRunId, onError: appendError })

const {
  applyEvent: applyRunLifecycleEvent,
  refresh: refreshRunLifecycle,
  reset: resetRunLifecycle,
  status: runStatus,
} = useRunLifecycle({ activeRunId, onError: appendError, sessionToken, threadId })

let resumeStream: () => Promise<void> = async () => undefined
const {
  approvalPayload,
  approve,
  handleBackendMessage: handleApprovalMessage,
  handleStreamApproval,
  isApprovalModalOpen,
  reject,
  resetApproval,
} = useApprovalFlow({
  onError: appendError,
  onResume: () => resumeStream(),
  sessionToken,
  socket,
  threadId,
})

const { isRefactoring, resetStream, sendStreamRequest } = useRefactorStream({
  activeRunId,
  applyEvent: (event) => {
    applyDagEvent(event)
    applyRunBudgetEvent(event)
    applyRunLifecycleEvent(event)
  },
  beginAgentResponse,
  ensureBackendSession,
  getRuntimeModelConfig: () => modelProviderRef.value?.getRuntimeModelConfig() ?? {},
  onApproval: handleStreamApproval,
  onLog: (log) => agentLogs.value.push(log),
  onPlanCreated: () => workspaceTabsRef.value?.handlePlanCreated(),
  onStreamSettled: () => {
    workspaceTabsRef.value?.refreshTopology()
    void refreshRunLifecycle()
  },
  resetInitialTurn: () => {
    resetRunScopeState(false)
  },
  sessionToken,
  threadId,
  updateAgentResponse,
})
resumeStream = () => sendStreamRequest('', false)

setMessageHandler((message) => {
  if (handleApprovalMessage(message)) return
  handleChatMessage(message)
})

const handleInitialRefactor = () => {
  if (!sourceCode.value.trim()) {
    alert('请输入代码内容或路径！')
    return
  }
  void sendStreamRequest(sourceCode.value, true)
}

const handleSendChatMessage = () => {
  if (isRefactoring.value) return
  const message = takeUserMessage()
  if (message) void sendStreamRequest(message, false)
}

/**
 * 运行作用域只有这一处销毁入口：组件状态、异步审批与传输代际一起失效。
 * 模型和主题属于用户配置，不参与 reset，因而新会话不会误删持久偏好。
 */
const resetRunScopeState = (cancelTransport = true) => {
  if (cancelTransport) resetStream()
  activeRunId.value = ''
  agentLogs.value = []
  resetApproval()
  resetChat()
  resetDag()
  resetRunBudget()
  resetRunLifecycle()
}

const handleNewSession = async () => {
  resetRunScopeState()
  try {
    await resetBackendSession()
  } catch (error) {
    reportSessionError(error)
  }
}

onMounted(async () => {
  try {
    await startBackendSession()
  } catch (error) {
    reportSessionError(error)
  }
})

onUnmounted(() => {
  resetRunScopeState()
  disposeBackendSession()
})
</script>

<template>
  <div class="app-container" :data-theme="resolvedTheme">
    <AppHeader
      :theme-options="themeOptions"
      :theme-preference="themePreference"
      @select-theme="setThemePreference"
    />

    <main class="main-content">
      <div class="column col-control">
        <SourceInputPanel
          v-model="sourceCode"
          :is-refactoring="isRefactoring"
          :thread-id="threadId"
          @new-session="handleNewSession"
          @submit="handleInitialRefactor"
        >
          <ModelProviderPanel ref="modelProviderRef" />
        </SourceInputPanel>
      </div>

      <AgentChatroom
        v-model="userChatInput"
        :is-refactoring="isRefactoring"
        :messages="chatMessages"
        @send="handleSendChatMessage"
      />

      <WorkspaceTabs
        ref="workspaceTabsRef"
        v-model:nodes="dagNodes"
        v-model:edges="dagEdges"
        :budget-exceeded-reason="exceededReason"
        :budget-limits="budgetLimits"
        :budget-usage="budgetUsage"
        :is-budget-exceeded="isBudgetExceeded"
        :logs="agentLogs"
        :run-status="runStatus"
        :theme="resolvedTheme"
      />
    </main>

    <ApprovalDiffModal
      :open="isApprovalModalOpen"
      :payload="approvalPayload"
      @approve="approve"
      @reject="reject"
    />
  </div>
</template>

<style src="./styles/workspace.css"></style>
