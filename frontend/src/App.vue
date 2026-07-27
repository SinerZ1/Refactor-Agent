<script setup lang="ts">
import { defineAsyncComponent, ref, nextTick, watch, onMounted, onUnmounted } from 'vue'
import { Marked } from 'marked'
import '@vue-flow/core/dist/style.css'
import '@vue-flow/core/dist/theme-default.css'
import { API_BASE_URL } from './api'
import { useModelProvider } from './composables/useModelProvider'
import { useRunBudget } from './composables/useRunBudget'
import { useTaskDag } from './composables/useTaskDag'
import { parseAgentEvent } from './types/agentEvents'
import { useTheme } from './composables/useTheme'

const markedInstance = new Marked({
  gfm: true,
  breaks: true,
})

const renderMarkdown = (text: string) => {
  if (!text) return ''
  return markedInstance.parse(text) as string
}

// 两个图形面板都不属于默认代码视图。异步组件使 ECharts/Vue Flow 在用户首次
// 打开对应标签时才下载，避免把图形引擎计入工作区首屏关键路径。
const VueFlow = defineAsyncComponent(() =>
  import('@vue-flow/core').then((module) => module.VueFlow),
)
const TopologyGraph = defineAsyncComponent(() => import('./components/TopologyGraph.vue'))

const { resolvedTheme, setThemePreference, themeOptions, themePreference } = useTheme()

// 基础状态
const sourceCode = ref('CodeSmells/main.py') // 默认要重构的测试文件路径
const refactoredCode = ref('')
const isRefactoring = ref(false)
let streamAbortController: AbortController | null = null
let streamGeneration = 0
const activeRunId = ref('')
const agentLogs = ref<{ type: 'info' | 'success' | 'error'; message: string; time: string }[]>([])

// 阶段 4：会话与多轮对话记忆状态
const threadId = ref('')
const sessionToken = ref('')
const userChatInput = ref('')
const chatMessages = ref<
  { role: 'user' | 'agent' | 'coder' | 'reviewer' | 'architect'; text: string }[]
>([])

const {
  activeModelName,
  activeModelOptions,
  adcStatus,
  canConnectProvider,
  connectModelProvider,
  connectionFeedback,
  currentApiKey,
  getRuntimeModelConfig,
  handleProviderChange,
  isConnectingProvider,
  loadAdcStatus,
  modelConfig,
  saveConfig,
} = useModelProvider()

// 阶段 2：WebSocket 双向全双工 & 人机协作审批 (HITL)
const socket = ref<WebSocket | null>(null)
const isApprovalModalOpen = ref(false)
interface ApprovalPayload {
  approval_id: string
  file_path: string
  original_code: string
  refactored_code: string
}
const approvalPayload = ref<ApprovalPayload | null>(null)

const parseApprovalPayload = (payload: Record<string, unknown> | undefined) => {
  if (
    !payload ||
    typeof payload.approval_id !== 'string' ||
    typeof payload.file_path !== 'string' ||
    typeof payload.original_code !== 'string' ||
    typeof payload.refactored_code !== 'string'
  ) {
    return null
  }
  return payload as unknown as ApprovalPayload
}

const createBackendSession = async () => {
  const response = await fetch(`${API_BASE_URL}/api/sessions`, { method: 'POST' })
  if (!response.ok) {
    throw new Error(`创建后端会话失败（HTTP ${response.status}）`)
  }
  const payload = (await response.json()) as {
    thread_id: string
    session_token: string
  }
  threadId.value = payload.thread_id
  sessionToken.value = payload.session_token
}

const ensureBackendSession = async () => {
  if (!threadId.value || !sessionToken.value) {
    await createBackendSession()
    initWebSocket()
  }
}

const cancelActiveStream = () => {
  streamGeneration += 1
  streamAbortController?.abort()
  streamAbortController = null
  isRefactoring.value = false
}

// 初始化 WebSocket 连接并监听
const initWebSocket = () => {
  if (!threadId.value || !sessionToken.value) return
  if (socket.value) {
    socket.value.close()
  }

  const apiUrl = new URL(API_BASE_URL)
  const wsProtocol = apiUrl.protocol === 'https:' ? 'wss:' : 'ws:'
  const wsUrl = `${wsProtocol}//${apiUrl.host}/ws/refactor/${encodeURIComponent(threadId.value)}?token=${encodeURIComponent(sessionToken.value)}`

  socket.value = new WebSocket(wsUrl)

  socket.value.onmessage = (event) => {
    try {
      const data = JSON.parse(event.data)
      const messageRunId = typeof data.run_id === 'string' ? data.run_id : ''
      if (messageRunId && activeRunId.value && messageRunId !== activeRunId.value) {
        return
      }
      if (messageRunId && !activeRunId.value) {
        activeRunId.value = messageRunId
      }

      if (data.type === 'approval_request') {
        // 挂起状态，显示 HITL 审批弹窗
        const payload = parseApprovalPayload(data.payload)
        if (payload) {
          approvalPayload.value = payload
          isApprovalModalOpen.value = true
        } else {
          agentLogs.value.push({
            type: 'error',
            message: '收到 WebSocket 审批请求但数据缺失',
            time: new Date().toLocaleTimeString(),
          })
        }
      } else if (data.type === 'approval_confirmed') {
        // 审批结果确认，隐藏弹窗，发起新的 SSE 重构流请求进行恢复
        isApprovalModalOpen.value = false
        void sendStreamRequest('', false)
      } else if (data.type === 'approval_error') {
        agentLogs.value.push({
          type: 'error',
          message: data.message || '审批请求已失效',
          time: new Date().toLocaleTimeString(),
        })
      } else if (data.type === 'chatroom_message') {
        // 阶段 3：A2A 多角色群聊，分发消息角色与头像
        const sender = data.sender
        const content = data.content
        let role: 'coder' | 'reviewer' | 'architect' = 'coder'
        if (sender === 'ReviewerAgent') role = 'reviewer'
        else if (sender === 'ArchitectAgent') role = 'architect'

        chatMessages.value.push({
          role: role,
          text: content,
        })
      }
    } catch (err) {
      console.error('[WebSocket] Failed to parse message:', err)
    }
  }

  socket.value.onclose = () => {
    console.log('[WebSocket] Disconnected.')
  }

  socket.value.onerror = (err) => {
    console.error('[WebSocket] Error:', err)
  }
}

const submitApprovalDecision = async (approved: boolean) => {
  if (!approvalPayload.value) return

  if (socket.value && socket.value.readyState === WebSocket.OPEN) {
    socket.value.send(
      JSON.stringify({
        type: 'approval_response',
        approval_id: approvalPayload.value.approval_id,
        approved,
      }),
    )
    return
  }

  const response = await fetch(
    `${API_BASE_URL}/api/sessions/${encodeURIComponent(threadId.value)}/approval`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        session_token: sessionToken.value,
        approval_id: approvalPayload.value.approval_id,
        approved,
      }),
    },
  )
  if (!response.ok) {
    const payload = (await response.json().catch(() => ({}))) as { detail?: string }
    throw new Error(payload.detail || `审批提交失败（HTTP ${response.status}）`)
  }
  isApprovalModalOpen.value = false
  await sendStreamRequest('', false)
}

// 批准写入修改
const handleApprove = async () => {
  try {
    await submitApprovalDecision(true)
  } catch (error) {
    agentLogs.value.push({
      type: 'error',
      message: error instanceof Error ? error.message : '审批提交失败',
      time: new Date().toLocaleTimeString(),
    })
  }
}

// 拒绝写入修改并打回
const handleReject = async () => {
  try {
    await submitApprovalDecision(false)
  } catch (error) {
    agentLogs.value.push({
      type: 'error',
      message: error instanceof Error ? error.message : '审批提交失败',
      time: new Date().toLocaleTimeString(),
    })
  }
}

// 挂载时启动
onMounted(async () => {
  try {
    await createBackendSession()
    initWebSocket()
  } catch (error) {
    agentLogs.value.push({
      type: 'error',
      message: error instanceof Error ? error.message : '无法创建后端会话',
      time: new Date().toLocaleTimeString(),
    })
  }
  loadAdcStatus()
})

onUnmounted(() => {
  cancelActiveStream()
  socket.value?.close()
  socket.value = null
})

// 阶段 6: 视图切换和拓扑组件引用
const activeTab = ref<'code' | 'dag' | 'topology'>('code')
const topologyGraphRef = ref<{
  refresh: () => Promise<void>
  resize: () => void
} | null>(null)
const taskDagGraphRef = ref<{
  fitView: (options?: { padding?: number }) => void
} | null>(null)

watch(activeTab, (newTab) => {
  if (newTab === 'topology') {
    nextTick(() => {
      topologyGraphRef.value?.resize()
    })
  } else if (newTab === 'dag') {
    nextTick(() => {
      taskDagGraphRef.value?.fitView({ padding: 0.15 })
    })
  }
})

const { applyDagEvent, dagEdges, dagNodes, resetDag } = useTaskDag()
const {
  applyRunBudgetEvent,
  exceededReason,
  isBudgetExceeded,
  limits: budgetLimits,
  resetRunBudget,
  usage: budgetUsage,
} = useRunBudget()

// 自动滚动控制
const logContainerRef = ref<HTMLDivElement | null>(null)
const chatContainerRef = ref<HTMLDivElement | null>(null)

watch(
  agentLogs,
  () => {
    nextTick(() => {
      if (logContainerRef.value) {
        logContainerRef.value.scrollTop = logContainerRef.value.scrollHeight
      }
    })
  },
  { deep: true },
)

watch(
  chatMessages,
  () => {
    nextTick(() => {
      if (chatContainerRef.value) {
        chatContainerRef.value.scrollTop = chatContainerRef.value.scrollHeight
      }
    })
  },
  { deep: true },
)

// 重置会话 (New Session)
const handleNewSession = async () => {
  cancelActiveStream()
  socket.value?.close()
  threadId.value = ''
  sessionToken.value = ''
  refactoredCode.value = ''
  userChatInput.value = ''
  chatMessages.value = []
  agentLogs.value = []
  activeRunId.value = ''
  resetRunBudget()
  try {
    await createBackendSession()
    initWebSocket()
  } catch (error) {
    agentLogs.value.push({
      type: 'error',
      message: error instanceof Error ? error.message : '无法创建后端会话',
      time: new Date().toLocaleTimeString(),
    })
  }
}

// 核心流式请求方法
const sendStreamRequest = async (payloadText: string, isInitialTurn: boolean) => {
  try {
    await ensureBackendSession()
  } catch (error) {
    agentLogs.value.push({
      type: 'error',
      message: error instanceof Error ? error.message : '无法创建后端会话',
      time: new Date().toLocaleTimeString(),
    })
    return
  }

  streamAbortController?.abort()
  const controller = new AbortController()
  streamAbortController = controller
  const generation = ++streamGeneration
  const requestThreadId = threadId.value
  const requestSessionToken = sessionToken.value
  isRefactoring.value = true
  if (isInitialTurn) {
    refactoredCode.value = ''
    chatMessages.value = []
    resetDag()
    resetRunBudget()
  }

  // 为本次对话在 Chat 中占个位
  const agentMessageIndex = chatMessages.value.length
  if (!isInitialTurn) {
    chatMessages.value.push({ role: 'agent', text: '正在思考...' })
  } else {
    chatMessages.value.push({ role: 'agent', text: '正在进行首次代码分析与重构...' })
  }

  let accumulatedResponse = ''

  try {
    const response = await fetch(`${API_BASE_URL}/api/refactor/stream`, {
      method: 'POST',
      signal: controller.signal,
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        code: payloadText,
        thread_id: requestThreadId,
        session_token: requestSessionToken,
        model_config: getRuntimeModelConfig(),
      }),
    })

    if (!response.ok) {
      throw new Error(`HTTP error! status: ${response.status}`)
    }

    const reader = response.body?.getReader()
    const decoder = new TextDecoder()
    if (!reader) {
      throw new Error('未获取到 Stream Reader')
    }

    let buffer = ''
    while (true) {
      const { value, done } = await reader.read()
      if (generation !== streamGeneration) {
        await reader.cancel()
        return
      }
      if (done) {
        break
      }

      buffer += decoder.decode(value, { stream: true })
      const lines = buffer.split('\n\n')
      buffer = lines.pop() || ''

      for (const line of lines) {
        if (line.trim().startsWith('data: ')) {
          try {
            const jsonStr = line.replace(/^data:\s*/, '')
            const event = parseAgentEvent(JSON.parse(jsonStr))
            if (!event) continue
            if (event.type === 'run.started' && event.run_id) {
              activeRunId.value = event.run_id
            } else if (event.run_id && activeRunId.value && event.run_id !== activeRunId.value) {
              // HTTP 流可以被 AbortController 取消，但已经排队的 WebSocket/SSE 数据仍
              // 可能晚到。run_id 是跨通道的 happens-before 边界，旧运行不得再覆写 UI。
              continue
            }

            applyDagEvent(event)
            applyRunBudgetEvent(event)
            if (event.type === 'plan.created' && activeTab.value === 'dag') {
              // Vue Flow 的 fit-view-on-init 只在首次挂载时执行。计划到达后节点数量和
              // 拓扑深度都会变化，因此显式重算视口，确保深层动态 DAG 不会落在画布外。
              nextTick(() => taskDagGraphRef.value?.fitView({ padding: 0.15 }))
            }
            if (event.type === 'agent.message.delta') {
              accumulatedResponse += event.message
              refactoredCode.value = accumulatedResponse
              if (chatMessages.value[agentMessageIndex]) {
                chatMessages.value[agentMessageIndex].text = accumulatedResponse
              }
            } else if (event.type === 'approval.waiting') {
              // SSE 是审批的可靠备用通道，WebSocket 继续承担实时双向交互。
              const payload = parseApprovalPayload(event.payload)
              if (payload) {
                approvalPayload.value = payload
                isApprovalModalOpen.value = true
              } else {
                agentLogs.value.push({
                  type: 'error',
                  message: '收到的审批事件缺少必要字段',
                  time: new Date().toLocaleTimeString(),
                })
              }
            } else {
              agentLogs.value.push({
                type: event.level,
                message: event.message,
                time: new Date().toLocaleTimeString(),
              })
            }
          } catch (e) {
            console.error('解析 SSE 失败:', line, e)
          }
        }
      }
    }
  } catch (error) {
    if (
      generation !== streamGeneration ||
      controller.signal.aborted ||
      (error instanceof DOMException && error.name === 'AbortError')
    ) {
      return
    }
    console.error('SSE Error:', error)
    agentLogs.value.push({
      type: 'error',
      message: `错误: ${error}`,
      time: new Date().toLocaleTimeString(),
    })
    if (chatMessages.value[agentMessageIndex]) {
      chatMessages.value[agentMessageIndex].text =
        `[重构失败] 无法完成此次对话，请检查后端运行状态。`
    }
  } finally {
    if (generation === streamGeneration) {
      streamAbortController = null
      isRefactoring.value = false
      // 流程结束后，自动刷新图谱以展示最新架构关系
      nextTick(() => {
        topologyGraphRef.value?.refresh()
      })
    }
  }
}

// 首次重构提交
const handleInitialRefactor = () => {
  if (!sourceCode.value.trim()) {
    alert('请输入代码内容或路径！')
    return
  }
  sendStreamRequest(sourceCode.value, true)
}

// 连续多轮对话提交
const handleSendChatMessage = () => {
  if (!userChatInput.value.trim() || isRefactoring.value) {
    return
  }
  const userText = userChatInput.value
  chatMessages.value.push({ role: 'user', text: userText })
  userChatInput.value = ''

  // 触发流式，作为 follow-up 信息发给 backend
  sendStreamRequest(userText, false)
}
</script>

<template>
  <div class="app-container" :data-theme="resolvedTheme">
    <header class="header">
      <div class="brand-block">
        <div class="brand-mark" aria-hidden="true">RA</div>
        <div class="brand-copy">
          <div class="brand-title-row">
            <h1>Refactor Agent</h1>
          </div>
        </div>
      </div>

      <div class="theme-control" role="group" aria-label="界面主题">
        <button
          v-for="option in themeOptions"
          :key="option.value"
          type="button"
          :class="['theme-option', { active: themePreference === option.value }]"
          :aria-pressed="themePreference === option.value"
          :title="option.label"
          @click="setThemePreference(option.value)"
        >
          <span aria-hidden="true">{{ option.icon }}</span>
          <span class="theme-option-label">{{ option.label }}</span>
        </button>
      </div>
    </header>

    <main class="main-content">
      <!-- 栏 1：控制与初始输入 -->
      <div class="column col-control">
        <div class="panel">
          <div class="panel-header">
            <h3><span class="panel-icon" aria-hidden="true">⌘</span> 初始代码/本地文件</h3>
          </div>
          <div class="panel-body flex-column">
            <textarea
              v-model="sourceCode"
              class="code-textarea"
              placeholder="在此粘贴代码或填入本地路径（如: CodeSmells/main.py）"
            ></textarea>

            <button
              @click="handleInitialRefactor"
              :disabled="isRefactoring"
              class="action-btn initial-btn"
            >
              <span v-if="isRefactoring" class="spinner"></span>
              {{ isRefactoring ? '分析重构中...' : '提交初始重构 👉' }}
            </button>
          </div>
        </div>

        <!-- 阶段 1：智能体模型配置卡片 -->
        <div class="panel settings-card">
          <div class="panel-header">
            <h3><span class="panel-icon" aria-hidden="true">⌘</span> 智能体模型配置</h3>
          </div>
          <div class="panel-body flex-column" style="gap: 0.8rem; overflow-y: auto">
            <div class="form-group">
              <label>Provider:</label>
              <select
                v-model="modelConfig.provider"
                @change="handleProviderChange"
                class="form-select"
              >
                <option value="openai">OpenAI API Compatible</option>
                <option value="gemini_studio">Gemini AI Studio</option>
                <option value="google_vertex">Google Vertex AI</option>
              </select>
            </div>

            <!-- OpenAI 兼容配置 -->
            <div v-if="modelConfig.provider === 'openai'" class="provider-sub-form">
              <div class="form-group">
                <label>API Key:</label>
                <input
                  v-model="currentApiKey"
                  type="password"
                  placeholder="sk-..."
                  autocomplete="off"
                  class="form-input"
                />
              </div>
              <div class="form-group" style="margin-top: 0.4rem">
                <label>Base URL:</label>
                <input
                  v-model="modelConfig.base_url"
                  @input="saveConfig"
                  type="text"
                  placeholder="https://api.deepseek.com"
                  class="form-input"
                />
              </div>
              <div class="form-group" style="margin-top: 0.4rem">
                <label>Model:</label>
                <input
                  v-model="activeModelName"
                  type="text"
                  list="available-model-options"
                  placeholder="deepseek-v4-flash"
                  class="form-input"
                />
              </div>
            </div>

            <!-- Gemini AI Studio 配置 -->
            <div v-if="modelConfig.provider === 'gemini_studio'" class="provider-sub-form">
              <div class="form-group">
                <label>API Key:</label>
                <input
                  v-model="currentApiKey"
                  type="password"
                  placeholder="AIza..."
                  autocomplete="off"
                  class="form-input"
                />
              </div>
              <div class="form-group" style="margin-top: 0.4rem">
                <label>Model:</label>
                <input
                  v-model="activeModelName"
                  type="text"
                  list="available-model-options"
                  placeholder="gemini-3.5-flash"
                  class="form-input"
                />
              </div>
            </div>

            <!-- Google Vertex AI 配置 -->
            <div
              v-if="modelConfig.provider === 'google_vertex'"
              class="provider-sub-form"
              style="display: flex; flex-direction: column; gap: 0.6rem"
            >
              <div class="form-group">
                <label>Project ID:</label>
                <input
                  v-model="modelConfig.vertex_project_id"
                  @input="saveConfig"
                  type="password"
                  placeholder="Google Cloud project ID"
                  autocomplete="off"
                  class="form-input"
                />
              </div>
              <div class="form-group">
                <label>Location:</label>
                <input
                  v-model="modelConfig.vertex_location"
                  @input="saveConfig"
                  type="text"
                  placeholder="global"
                  class="form-input"
                />
              </div>
              <div class="form-group">
                <label>Model:</label>
                <input
                  v-model="activeModelName"
                  type="text"
                  list="available-model-options"
                  placeholder="gemini-3.5-flash"
                  class="form-input"
                />
              </div>
              <div class="form-group">
                <label>Auth Mode:</label>
                <select
                  v-model="modelConfig.vertex_auth_mode"
                  @change="handleProviderChange"
                  class="form-select"
                >
                  <option value="adc">Application Default Credentials (ADC)</option>
                  <option value="api_key">API Key</option>
                </select>
              </div>
              <div v-if="modelConfig.vertex_auth_mode === 'adc'" class="adc-status-card">
                <span :class="['status-dot', adcStatus.available ? 'success' : 'error']"></span>
                <div>
                  <strong>ADC</strong>
                  <p>{{ adcStatus.message }}</p>
                  <small v-if="adcStatus.available && adcStatus.credential_type">
                    {{ adcStatus.credential_type }} · {{ adcStatus.source || 'default' }}
                  </small>
                </div>
                <button type="button" class="adc-refresh-btn" @click="loadAdcStatus">检查</button>
              </div>
              <div v-if="modelConfig.vertex_auth_mode === 'api_key'" class="form-group">
                <label>API Key:</label>
                <input
                  v-model="currentApiKey"
                  type="password"
                  placeholder="Google Cloud API key"
                  autocomplete="off"
                  class="form-input"
                />
              </div>
            </div>

            <datalist id="available-model-options">
              <option v-for="model in activeModelOptions" :key="model" :value="model"></option>
            </datalist>

            <div class="connection-actions">
              <button
                type="button"
                class="connect-btn"
                :disabled="isConnectingProvider || !canConnectProvider"
                @click="connectModelProvider"
              >
                <span v-if="isConnectingProvider" class="spinner"></span>
                {{ isConnectingProvider ? '连接中…' : '连接' }}
              </button>
              <p
                v-if="connectionFeedback.message"
                :class="['connection-feedback', connectionFeedback.type]"
                role="status"
              >
                {{ connectionFeedback.message }}
              </p>
            </div>
          </div>
        </div>

        <!-- 阶段 4 记忆卡片 -->
        <div class="panel session-card">
          <div class="panel-header">
            <h3><span class="panel-icon" aria-hidden="true">◇</span> 会话记忆控制</h3>
          </div>
          <div class="panel-body">
            <div class="session-info">
              <span class="label">会话 ID:</span>
              <code class="session-id">{{ threadId }}</code>
            </div>
            <button @click="handleNewSession" class="action-btn new-session-btn">
              🔄 开启新会话 (重置记忆)
            </button>
          </div>
        </div>
      </div>

      <!-- 栏 2：多轮 Chat 对话区 -->
      <div class="column col-chat">
        <div class="panel chat-panel">
          <div class="panel-header">
            <h3><span class="panel-icon" aria-hidden="true">⌘</span> 多轮交互重构对话</h3>
          </div>

          <div ref="chatContainerRef" class="chat-body">
            <div v-if="chatMessages.length === 0" class="empty-chat">
              请先在左侧提交初始重构。重构完成后，你可以在此处连续对 Agent 发送追问（例如：“再重命名
              add 方法”、“写一个对应的单元测试”）。
            </div>

            <div
              v-for="(msg, index) in chatMessages"
              :key="index"
              :class="['chat-bubble', msg.role]"
            >
              <div class="avatar">
                <span v-if="msg.role === 'user'">👤 用户</span>
                <span v-else-if="msg.role === 'agent'">🤖 Agent</span>
                <span v-else-if="msg.role === 'coder'">🧑‍💻 CoderAgent (Developer)</span>
                <span v-else-if="msg.role === 'reviewer'">🛡️ ReviewerAgent (Reviewer)</span>
                <span v-else-if="msg.role === 'architect'">📐 ArchitectAgent (Architect)</span>
                <span v-else>🤖 {{ msg.role }}</span>
              </div>
              <div class="bubble-markdown" v-html="renderMarkdown(msg.text)"></div>
            </div>
          </div>

          <div class="chat-footer">
            <input
              v-model="userChatInput"
              @keydown.enter="handleSendChatMessage"
              :disabled="isRefactoring || chatMessages.length === 0"
              type="text"
              placeholder="发送后续重构修改建议（例如: 优化代码结构、重命名变量等）..."
              class="chat-input"
            />
            <button
              @click="handleSendChatMessage"
              :disabled="isRefactoring || !userChatInput.trim() || chatMessages.length === 0"
              class="chat-send-btn"
            >
              发送
            </button>
          </div>
        </div>
      </div>

      <!-- 栏 3：日志与最新代码 / 拓扑图谱 (阶段 6 Tab页) -->
      <div class="column col-display">
        <!-- Tab 切换头部 -->
        <div class="tab-header">
          <button
            :class="['tab-btn', activeTab === 'code' ? 'active' : '']"
            @click="activeTab = 'code'"
          >
            <span aria-hidden="true">⌘</span> 运行日志
          </button>
          <button
            :class="['tab-btn', activeTab === 'dag' ? 'active' : '']"
            @click="activeTab = 'dag'"
          >
            <span aria-hidden="true">◇</span> 任务 DAG
          </button>
          <button
            :class="['tab-btn', activeTab === 'topology' ? 'active' : '']"
            @click="activeTab = 'topology'"
          >
            <span aria-hidden="true">⌘</span> 依赖图谱
          </button>
        </div>

        <!-- 3-A: 代码与日志视图 -->
        <div
          v-show="activeTab === 'code'"
          class="tab-content flex-column"
          style="gap: 0.8rem; height: calc(100% - 44px)"
        >
          <!-- 3-1: 运行日志 -->
          <div class="panel display-full" style="flex: 1; height: 100%">
            <div class="panel-header">
              <h3><span class="panel-icon" aria-hidden="true">⌘</span> Agent 思考与工具调用日志</h3>
            </div>
            <div ref="logContainerRef" class="log-content">
              <div v-if="agentLogs.length === 0" class="empty-logs">等待 Agent 执行操作...</div>
              <div v-for="(log, idx) in agentLogs" :key="idx" :class="['log-item', log.type]">
                <span class="log-time">[{{ log.time }}]</span>
                <pre class="log-message">{{ log.message }}</pre>
              </div>
            </div>
          </div>
        </div>

        <!-- 3-B: 重构任务 DAG 看板视图 (Vue Flow) -->
        <div v-if="activeTab === 'dag'" class="tab-content" style="height: calc(100% - 44px)">
          <div class="panel" style="height: 100%">
            <div class="topology-toolbar">
              <span class="title"
                ><span class="panel-icon" aria-hidden="true">◇</span> 重构任务 DAG 进度看板</span
              >
              <span class="badge-neo4j" style="background-color: #0b533e; margin-left: 10px"
                >任务编排</span
              >
              <span
                v-if="budgetUsage && budgetLimits"
                class="badge-neo4j"
                :title="
                  isBudgetExceeded
                    ? exceededReason
                    : `未计量模型回合：${budgetUsage.unmetered_steps}`
                "
                :style="{
                  backgroundColor: isBudgetExceeded ? '#7f1d1d' : '#1e3a5f',
                  marginLeft: '10px',
                }"
              >
                预算 {{ budgetUsage.agent_steps }}/{{ budgetLimits.max_agent_steps }} 步 ·
                {{ budgetUsage.tool_calls }}/{{ budgetLimits.max_tool_calls }} 工具 ·
                {{ budgetUsage.total_tokens.toLocaleString() }}/{{
                  budgetLimits.max_total_tokens.toLocaleString()
                }}
                Token
              </span>
            </div>
            <div style="flex: 1; width: 100%; height: 100%; min-height: 350px">
              <VueFlow
                ref="taskDagGraphRef"
                v-model:nodes="dagNodes"
                v-model:edges="dagEdges"
                :fit-view-on-init="true"
                :nodes-draggable="true"
                :zoom-on-scroll="true"
                :zoom-on-pinch="true"
                :zoom-on-double-click="false"
              />
            </div>
          </div>
        </div>

        <!-- 3-B: 拓扑图谱视图 -->
        <div v-if="activeTab === 'topology'" class="tab-content" style="height: calc(100% - 44px)">
          <div class="panel" style="height: 100%">
            <TopologyGraph ref="topologyGraphRef" :theme="resolvedTheme" />
          </div>
        </div>
      </div>
    </main>

    <!-- 阶段 2：人机协作审批 (HITL) 弹窗 -->
    <div v-if="isApprovalModalOpen && approvalPayload" class="modal-overlay">
      <div class="modal-container">
        <div class="modal-header">
          <h3>
            <span class="panel-icon" aria-hidden="true">◇</span> 人机协作审批 (HITL) —— 代码修改确认
          </h3>
          <span class="file-badge">{{ approvalPayload.file_path }}</span>
        </div>

        <div class="modal-body">
          <p class="modal-tip">
            Developer Agent 申请写入文件。为了系统的安全和质量，请审查以下原代码与重构代码的对比。
          </p>

          <div class="diff-container">
            <!-- 左栏：原代码 -->
            <div class="diff-panel original">
              <div class="diff-panel-title">原代码 (Original)</div>
              <pre
                class="diff-pre"
              ><code>{{ approvalPayload.original_code || '# 这是一个新建的文件，原代码为空。' }}</code></pre>
            </div>

            <!-- 右栏：重构代码 -->
            <div class="diff-panel modified">
              <div class="diff-panel-title">重构代码 (Refactored)</div>
              <pre class="diff-pre"><code>{{ approvalPayload.refactored_code }}</code></pre>
            </div>
          </div>
        </div>

        <div class="modal-footer">
          <button @click="handleReject" class="modal-btn btn-reject">❌ 拒绝并退回</button>
          <button @click="handleApprove" class="modal-btn btn-approve">🟢 批准写入放行</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.app-container {
  display: flex;
  flex-direction: column;
  height: 100vh;
  font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
  background-color: #1e1e1e;
  color: #d4d4d4;
}

.header {
  padding: 0.8rem 2rem;
  background-color: #2d2d2d;
  border-bottom: 1px solid #3d3d3d;
  text-align: center;
}

.header h1 {
  margin: 0;
  font-size: 1.6rem;
  color: #4fc08d;
}

.header p {
  margin: 0.3rem 0 0;
  font-size: 0.9rem;
  color: #9cdcfe;
}

.main-content {
  display: flex;
  flex: 1;
  padding: 0.8rem;
  gap: 0.8rem;
  overflow: hidden;
}

/* 栏容器规划 */
.column {
  display: flex;
  flex-direction: column;
  gap: 0.8rem;
  height: 100%;
}

.col-control {
  flex: 1; /* 25% */
  min-width: 250px;
}

.col-chat {
  flex: 1.4; /* 35% */
  min-width: 320px;
}

.col-display {
  flex: 1.6; /* 40% */
  min-width: 380px;
}

/* 容器/面板规范 */
.panel {
  display: flex;
  flex-direction: column;
  background-color: #252526;
  border: 1px solid #3d3d3d;
  border-radius: 8px;
  overflow: hidden;
}

.panel-header {
  padding: 0.6rem 0.8rem;
  background-color: #333333;
  border-bottom: 1px solid #3d3d3d;
}

.panel-header h3 {
  margin: 0;
  font-size: 0.95rem;
  color: #dcdcaa;
}

.panel-body {
  padding: 0.8rem;
  flex: 1;
}

.flex-column {
  display: flex;
  flex-direction: column;
}

/* Tab 样式 */
.tab-header {
  display: flex;
  background-color: #2d2d2d;
  border: 1px solid #3d3d3d;
  border-radius: 8px;
  overflow: hidden;
  height: 36px;
  flex-shrink: 0;
}

.tab-btn {
  flex: 1;
  background: transparent;
  border: none;
  color: #888;
  font-size: 0.85rem;
  font-weight: bold;
  cursor: pointer;
  transition: all 0.2s;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 0.4rem;
}

.tab-btn:hover {
  background-color: #333;
  color: #ddd;
}

.tab-btn.active {
  background-color: #3c3c3c;
  color: #4fc08d;
  border-bottom: 2px solid #4fc08d;
}

.tab-content {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

/* 拓扑工具栏 */
.topology-toolbar {
  display: flex;
  align-items: center;
  padding: 0.6rem 0.8rem;
  background-color: #2d2d2d;
  border-bottom: 1px solid #3d3d3d;
}

.topology-toolbar .title {
  font-size: 0.9rem;
  font-weight: bold;
  color: #dcdcaa;
}

.badge-neo4j {
  font-size: 10px;
  background-color: #0b533e;
  color: #fff;
  padding: 2px 6px;
  border-radius: 4px;
}

/* 控制栏专属 */
.col-control .panel:first-child {
  flex: 1;
}

.code-textarea {
  flex: 1;
  width: 100%;
  padding: 0.8rem;
  background-color: #1e1e1e;
  color: #d4d4d4;
  border: 1px solid #333;
  border-radius: 4px;
  resize: none;
  font-family: 'Fira Code', 'Courier New', monospace;
  font-size: 13px;
  line-height: 1.4;
  outline: none;
  margin-bottom: 0.8rem;
}

.session-card {
  background-color: #202021;
}

.session-info {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  margin-bottom: 0.8rem;
}

.session-info .label {
  font-size: 0.85rem;
  color: #888;
}

.session-id {
  font-family: monospace;
  background-color: #111;
  padding: 0.2rem 0.4rem;
  border-radius: 4px;
  color: #ffaa00;
  font-size: 0.85rem;
}

.action-btn {
  padding: 0.7rem 1.2rem;
  font-size: 0.95rem;
  font-weight: bold;
  color: #fff;
  border: none;
  border-radius: 4px;
  cursor: pointer;
  transition: all 0.2s;
}

.initial-btn {
  background-color: #4fc08d;
}

.initial-btn:hover:not(:disabled) {
  background-color: #3aa876;
}

.new-session-btn {
  background-color: #3c3c3c;
  width: 100%;
}

.new-session-btn:hover {
  background-color: #4c4c4c;
}

/* 聊天栏专属 */
.chat-panel {
  flex: 1;
}

.chat-body {
  flex: 1;
  padding: 1rem;
  overflow-y: auto;
  background-color: #1a1a1b;
  display: flex;
  flex-direction: column;
  gap: 1rem;
}

.empty-chat {
  color: #555;
  font-style: italic;
  text-align: center;
  margin-top: 4rem;
  font-size: 0.9rem;
  line-height: 1.6;
}

.chat-bubble {
  display: flex;
  flex-direction: column;
  max-width: 90%;
  padding: 0.8rem;
  border-radius: 8px;
  animation: fadeIn 0.2s ease;
}

.chat-bubble.user {
  align-self: flex-end;
  background-color: #0b533e;
  color: #fff;
}

.chat-bubble.agent {
  align-self: flex-start;
  background-color: #2d2d30;
  color: #d4d4d4;
  border: 1px solid #3d3d3d;
}

.chat-bubble .avatar {
  font-size: 0.75rem;
  font-weight: bold;
  color: #888;
  margin-bottom: 0.3rem;
}

.chat-bubble.user .avatar {
  color: #4fc08d;
  align-self: flex-end;
}

.bubble-markdown {
  margin: 0;
  word-break: break-word;
  font-size: 13.5px;
  line-height: 1.5;
}

:deep(.bubble-markdown p) {
  margin: 0 0 0.5em;
}

:deep(.bubble-markdown p:last-child) {
  margin-bottom: 0;
}

:deep(.bubble-markdown pre) {
  background-color: var(--code-pre-bg, #f5f5f5);
  border: var(--code-pre-border, 1px solid #ccc);
  border-radius: 6px;
  padding: 0.6rem 0.8rem;
  margin: 0.5em 0;
  overflow-x: auto;
  font-family: 'Fira Code', 'Courier New', monospace;
  font-size: 12.5px;
  line-height: 1.4;
}

:deep(.bubble-markdown code) {
  font-family: 'Fira Code', 'Courier New', monospace;
  font-size: 0.9em;
  background-color: var(--code-inline-bg, #f8f9fa);
  padding: 0.15em 0.35em;
  border-radius: 4px;
}

:deep(.bubble-markdown pre code) {
  background-color: transparent;
  padding: 0;
  border-radius: 0;
}

:deep(.bubble-markdown ul),
:deep(.bubble-markdown ol) {
  margin: 0.4em 0;
  padding-left: 1.4em;
}

:deep(.bubble-markdown li) {
  margin-bottom: 0.2em;
}

:deep(.bubble-markdown blockquote) {
  margin: 0.5em 0;
  padding: 0.4em 0.8em;
  border-left: 3px solid #4fc08d;
  background-color: rgba(255, 255, 255, 0.05);
  color: inherit;
  opacity: 0.85;
}

:deep(.bubble-markdown table) {
  border-collapse: collapse;
  width: 100%;
  margin: 0.5em 0;
  font-size: 12.5px;
}

:deep(.bubble-markdown th),
:deep(.bubble-markdown td) {
  border: 1px solid rgba(255, 255, 255, 0.15);
  padding: 0.35rem 0.6rem;
  text-align: left;
}

:deep(.bubble-markdown th) {
  background-color: rgba(0, 0, 0, 0.2);
  font-weight: bold;
}

:deep(.bubble-markdown a) {
  color: #4fc08d;
  text-decoration: underline;
}

.chat-footer {
  display: flex;
  padding: 0.6rem;
  background-color: #2d2d2d;
  border-top: 1px solid #3d3d3d;
  gap: 0.5rem;
}

.chat-input {
  flex: 1;
  background-color: #1e1e1e;
  border: 1px solid #3d3d3d;
  border-radius: 4px;
  color: #d4d4d4;
  padding: 0.6rem;
  font-size: 0.9rem;
  outline: none;
}

.chat-send-btn {
  background-color: #4fc08d;
  color: white;
  border: none;
  padding: 0.6rem 1.2rem;
  font-weight: bold;
  border-radius: 4px;
  cursor: pointer;
}

.chat-send-btn:hover:not(:disabled) {
  background-color: #3aa876;
}

.chat-send-btn:disabled {
  background-color: #555;
  color: #888;
  cursor: not-allowed;
}

/* 终端风格的日志样式 */
.log-content {
  flex: 1;
  background-color: #111;
  padding: 0.8rem;
  overflow-y: auto;
  font-family: 'Fira Code', 'Courier New', monospace;
  font-size: 12px;
  line-height: 1.4;
}

.empty-logs {
  color: #444;
  font-style: italic;
  text-align: center;
  margin-top: 2rem;
}

.log-item {
  margin-bottom: 0.5rem;
  padding-bottom: 0.5rem;
  border-bottom: 1px dashed #222;
}

.log-item.info {
  color: #9cdcfe;
}

.log-item.success {
  color: #4fc08d;
}

.log-item.error {
  color: #f44336;
}

.log-time {
  color: #555;
  margin-right: 0.5rem;
}

.log-message {
  margin: 0.2rem 0 0;
  white-space: pre-wrap;
  word-break: break-all;
  background: transparent;
  padding: 0;
  font-family: inherit;
}

/* Spinner */
.spinner {
  display: inline-block;
  width: 14px;
  height: 14px;
  border: 2px solid rgba(255, 255, 255, 0.3);
  border-top-color: #fff;
  border-radius: 50%;
  animation: spin 1s linear infinite;
  margin-right: 0.3rem;
}

@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}

@keyframes fadeIn {
  from {
    opacity: 0;
    transform: translateY(5px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}

/* 阶段 1：智能体模型配置表单样式 */
.settings-card {
  max-height: 480px;
}

.form-group {
  display: flex;
  flex-direction: column;
  gap: 0.3rem;
  width: 100%;
}

.form-group label {
  font-size: 0.8rem;
  color: #888;
  font-weight: bold;
}

.form-select,
.form-input {
  background-color: #1e1e1e;
  border: 1px solid #3d3d3d;
  border-radius: 4px;
  color: #d4d4d4;
  padding: 0.45rem 0.6rem;
  font-size: 0.85rem;
  outline: none;
  box-sizing: border-box;
  width: 100%;
}

.form-select:focus,
.form-input:focus {
  border-color: #4fc08d;
  box-shadow: 0 0 3px rgba(79, 192, 141, 0.4);
}

.provider-sub-form {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}

.connection-actions {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
  padding-top: 0.15rem;
}

.connect-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-height: 38px;
  border: 0;
  border-radius: 8px;
  background: var(--primary);
  color: #fff;
  cursor: pointer;
  font: inherit;
  font-size: 0.84rem;
  font-weight: 720;
  transition:
    background-color 160ms ease,
    transform 160ms ease,
    opacity 160ms ease;
}

.connect-btn:hover:not(:disabled) {
  background: var(--primary-hover);
  transform: translateY(-1px);
}

.connect-btn:disabled {
  background: var(--surface-strong);
  color: var(--subtle);
  cursor: not-allowed;
}

.connection-feedback {
  margin: 0;
  padding: 0.48rem 0.58rem;
  border-radius: 7px;
  font-size: 0.72rem;
  line-height: 1.45;
}

.connection-feedback.success {
  background: var(--accent-soft);
  color: var(--success);
}

.connection-feedback.error {
  background: var(--original-bg);
  color: var(--danger);
}

.adc-status-card {
  display: flex;
  align-items: flex-start;
  gap: 0.55rem;
  padding: 0.6rem;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--surface-muted);
  color: var(--text-soft);
}

.adc-status-card strong {
  font-size: 0.75rem;
}

.adc-status-card p {
  margin: 0.12rem 0 0;
  color: var(--muted);
  font-size: 0.7rem;
  line-height: 1.4;
}

.adc-status-card small {
  display: block;
  margin-top: 0.16rem;
  color: var(--subtle);
  font-size: 0.64rem;
}

.adc-refresh-btn {
  flex: 0 0 auto;
  margin-left: auto;
  padding: 0.2rem 0.42rem;
  border: 1px solid var(--border-strong);
  border-radius: 6px;
  background: var(--surface);
  color: var(--primary);
  cursor: pointer;
  font: inherit;
  font-size: 0.64rem;
  font-weight: 700;
}

.adc-refresh-btn:hover {
  background: var(--primary-soft);
}

.status-dot {
  flex: 0 0 auto;
  width: 8px;
  height: 8px;
  margin-top: 0.3rem;
  border-radius: 50%;
  background: var(--subtle);
  box-shadow: 0 0 0 3px var(--surface-strong);
}

.status-dot.success {
  background: var(--success);
}

.status-dot.error {
  background: var(--danger);
}

/* 阶段 2：HITL 审批弹窗样式 */
.modal-overlay {
  position: fixed;
  top: 0;
  left: 0;
  width: 100vw;
  height: 100vh;
  background-color: rgba(0, 0, 0, 0.75);
  display: flex;
  justify-content: center;
  align-items: center;
  z-index: 9999;
}

.modal-container {
  width: 85%;
  height: 80%;
  background-color: #252526;
  border: 1px solid #4fc08d;
  border-radius: 12px;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  box-shadow: 0 10px 30px rgba(0, 0, 0, 0.5);
  animation: scaleIn 0.25s cubic-bezier(0.16, 1, 0.3, 1);
}

.modal-header {
  padding: 1rem 1.5rem;
  background-color: #333;
  border-bottom: 1px solid #3d3d3d;
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.modal-header h3 {
  margin: 0;
  font-size: 1.1rem;
  color: #4fc08d;
}

.file-badge {
  background-color: #1a1a1a;
  border: 1px solid #555;
  color: #ffaa00;
  font-family: monospace;
  font-size: 0.85rem;
  padding: 0.2rem 0.6rem;
  border-radius: 4px;
}

.modal-body {
  flex: 1;
  padding: 1.2rem 1.5rem;
  display: flex;
  flex-direction: column;
  gap: 1rem;
  overflow: hidden;
}

.modal-tip {
  margin: 0;
  font-size: 0.9rem;
  color: #aaa;
}

.diff-container {
  flex: 1;
  display: flex;
  gap: 1rem;
  overflow: hidden;
}

.diff-panel {
  flex: 1;
  display: flex;
  flex-direction: column;
  border-radius: 6px;
  overflow: hidden;
  border: 1px solid #3d3d3d;
}

.diff-panel.original {
  background-color: #1f1414;
}

.diff-panel.modified {
  background-color: #121f18;
}

.diff-panel-title {
  padding: 0.4rem 0.8rem;
  font-size: 0.8rem;
  font-weight: bold;
}

.diff-panel.original .diff-panel-title {
  background-color: #3d1b1b;
  color: #f44336;
}

.diff-panel.modified .diff-panel-title {
  background-color: #1c3d23;
  color: #4fc08d;
}

.diff-pre {
  margin: 0;
  padding: 0.8rem;
  flex: 1;
  overflow: auto;
  font-family: 'Fira Code', 'Courier New', monospace;
  font-size: 12px;
  line-height: 1.4;
  color: #ddd;
}

.modal-footer {
  padding: 1rem 1.5rem;
  background-color: #2d2d2d;
  border-top: 1px solid #3d3d3d;
  display: flex;
  justify-content: flex-end;
  gap: 1rem;
}

.modal-btn {
  padding: 0.6rem 1.5rem;
  font-size: 0.9rem;
  font-weight: bold;
  color: white;
  border: none;
  border-radius: 4px;
  cursor: pointer;
  transition: opacity 0.2s;
}

.modal-btn:hover {
  opacity: 0.9;
}

.btn-reject {
  background-color: #c62828;
}

.btn-approve {
  background-color: #2e7d32;
}

@keyframes scaleIn {
  from {
    transform: scale(0.95);
    opacity: 0;
  }
  to {
    transform: scale(1);
    opacity: 1;
  }
}

/* 阶段 3: A2A 多角色群聊气泡样式 */
.chat-bubble.coder {
  align-self: flex-start;
  background-color: #2b2a1a;
  color: #ffd700;
  border: 1px solid #d4af37;
}
.chat-bubble.reviewer {
  align-self: flex-start;
  background-color: #261622;
  color: #e040fb;
  border: 1px solid #ba68c8;
}
.chat-bubble.architect {
  align-self: flex-start;
  background-color: #16242d;
  color: #00e5ff;
  border: 1px solid #4dd0e1;
}
.chat-bubble.coder .avatar {
  color: #ffd700 !important;
}
.chat-bubble.reviewer .avatar {
  color: #e040fb !important;
}
.chat-bubble.architect .avatar {
  color: #00e5ff !important;
}

/* 阶段 4：Vue Flow DAG 样式与动效 */
:deep(.vue-flow) {
  background-color: #1a1a1a !important;
}

:deep(.vue-flow__node) {
  border-radius: 8px !important;
  font-size: 12.5px !important;
  font-weight: bold !important;
  padding: 12px !important;
  text-align: center !important;
  box-shadow: 0 4px 6px rgba(0, 0, 0, 0.4) !important;
  width: 210px !important;
  transition: all 0.3s ease !important;
  color: #eee !important;
}

:deep(.dag-node-pending) {
  background-color: #2a2a2a !important;
  color: #999 !important;
  border: 2px solid #444 !important;
}

:deep(.dag-node-in_progress) {
  background-color: #3e2723 !important;
  color: #ffb74d !important;
  border: 2px solid #ff9800 !important;
  animation: breathing 1.5s infinite ease-in-out !important;
}

:deep(.dag-node-completed) {
  background-color: #1b5e20 !important;
  color: #81c784 !important;
  border: 2px solid #4fc08d !important;
}

:deep(.dag-node-failed) {
  background-color: #b71c1c !important;
  color: #e57373 !important;
  border: 2px solid #f44336 !important;
  animation: shaking 0.4s ease-in-out !important;
}

@keyframes breathing {
  0% {
    box-shadow: 0 0 4px #ff9800;
  }
  50% {
    box-shadow: 0 0 16px #ff9800;
  }
  100% {
    box-shadow: 0 0 4px #ff9800;
  }
}

@keyframes shaking {
  0% {
    transform: translateX(0);
  }
  25% {
    transform: translateX(-5px);
  }
  50% {
    transform: translateX(5px);
  }
  75% {
    transform: translateX(-5px);
  }
  100% {
    transform: translateX(0);
  }
}
</style>

<style scoped>
/*
 * 主题令牌是本次界面改造的单一视觉来源：组件只消费语义色，不关心当前是
 * 浅色还是深色。这与 Agent 工作流的状态解耦思路一致——主题偏好属于输入，
 * resolvedTheme 才是渲染状态，系统主题变化无需侵入任何业务组件。
 */
.app-container[data-theme='light'] {
  color-scheme: light;
  --page-bg: #f3f6fb;
  --surface: #ffffff;
  --surface-muted: #f7f9fd;
  --surface-strong: #edf1f8;
  --header-tint: #eef2ff;
  --border: #dce3ef;
  --border-strong: #c9d3e3;
  --text: #172033;
  --text-soft: #42526a;
  --muted: #6b7b93;
  --subtle: #94a3b8;
  --primary: #4f46e5;
  --primary-hover: #4338ca;
  --primary-soft: #eef2ff;
  --primary-border: #c7d2fe;
  --accent: #0f766e;
  --accent-soft: #ccfbf1;
  --success: #07835f;
  --warning: #b45309;
  --danger: #dc2626;
  --terminal: #f8fafc;
  --terminal-text: #334155;
  --terminal-muted: #94a3b8;
  --log-info: #0369a1;
  --log-success: #047857;
  --log-error: #be123c;
  --shadow: 0 12px 30px rgba(51, 65, 85, 0.08);
  --shadow-focus: 0 0 0 3px rgba(79, 70, 229, 0.16);
  --user-bubble: #4f46e5;
  --agent-bubble: #f1f5f9;
  --coder-bg: #fffbeb;
  --coder-text: #92400e;
  --coder-border: #fcd34d;
  --reviewer-bg: #faf5ff;
  --reviewer-text: #7e22ce;
  --reviewer-border: #d8b4fe;
  --architect-bg: #eff6ff;
  --architect-text: #1d4ed8;
  --architect-border: #93c5fd;
  --modal-overlay: rgba(15, 23, 42, 0.55);
  --original-bg: #fff7f7;
  --original-title: #fee2e2;
  --modified-bg: #f0fdf7;
  --modified-title: #d1fae5;
  --code-pre-bg: #f5f5f5;
  --code-pre-border: 1px solid #ccc;
  --code-inline-bg: #f8f9fa;
}

.app-container[data-theme='dark'] {
  color-scheme: dark;
  --page-bg: #0b1020;
  --surface: #151b2b;
  --surface-muted: #111827;
  --surface-strong: #1c2639;
  --header-tint: #171b38;
  --border: #2b3850;
  --border-strong: #3b4a66;
  --text: #e8eef8;
  --text-soft: #c2cde0;
  --muted: #93a4bd;
  --subtle: #66758e;
  --primary: #818cf8;
  --primary-hover: #a5b4fc;
  --primary-soft: #252b52;
  --primary-border: #444e8f;
  --accent: #2dd4bf;
  --accent-soft: #123b3b;
  --success: #34d399;
  --warning: #fbbf24;
  --danger: #fb7185;
  --terminal: #090e1a;
  --terminal-text: #c8d4e8;
  --terminal-muted: #63738d;
  --log-info: #7dd3fc;
  --log-success: #6ee7b7;
  --log-error: #fda4af;
  --shadow: 0 16px 36px rgba(0, 0, 0, 0.24);
  --shadow-focus: 0 0 0 3px rgba(129, 140, 248, 0.22);
  --user-bubble: #4f46e5;
  --agent-bubble: #1d273a;
  --coder-bg: #332b18;
  --coder-text: #fcd34d;
  --coder-border: #8b6b24;
  --reviewer-bg: #30203b;
  --reviewer-text: #e9b4ff;
  --reviewer-border: #754991;
  --architect-bg: #172d43;
  --architect-text: #7dd3fc;
  --architect-border: #32688d;
  --modal-overlay: rgba(2, 6, 23, 0.8);
  --original-bg: #27191e;
  --original-title: #46232c;
  --modified-bg: #122820;
  --modified-title: #174333;
  --code-pre-bg: rgba(0, 0, 0, 0.3);
  --code-pre-border: 1px solid rgba(255, 255, 255, 0.12);
  --code-inline-bg: rgba(0, 0, 0, 0.2);
}

:global(html),
:global(body),
:global(#app) {
  width: 100%;
  min-width: 320px;
  height: 100%;
  margin: 0;
}

:global(body) {
  overflow: hidden;
}

:global(*) {
  box-sizing: border-box;
}

.app-container {
  min-height: 100vh;
  background:
    radial-gradient(circle at 50% -20%, var(--header-tint) 0, transparent 34%), var(--page-bg);
  color: var(--text);
  font-family:
    Inter,
    ui-sans-serif,
    system-ui,
    -apple-system,
    BlinkMacSystemFont,
    'Segoe UI',
    sans-serif;
  transition:
    background-color 180ms ease,
    color 180ms ease;
}

.header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 1rem;
  min-height: 74px;
  padding: 0.85rem 1.15rem;
  background: linear-gradient(115deg, var(--surface) 35%, var(--header-tint));
  border-bottom: 1px solid var(--border);
  text-align: left;
  box-shadow: 0 1px 0 rgba(15, 23, 42, 0.02);
}

.brand-block,
.brand-title-row {
  display: flex;
  align-items: center;
}

.brand-block {
  min-width: 0;
  gap: 0.8rem;
}

.brand-mark {
  display: grid;
  flex: 0 0 auto;
  width: 42px;
  height: 42px;
  place-items: center;
  border-radius: 13px;
  background: linear-gradient(135deg, var(--primary), var(--accent));
  color: #ffffff;
  font-size: 0.8rem;
  font-weight: 800;
  letter-spacing: 0.05em;
  box-shadow: 0 8px 20px rgba(79, 70, 229, 0.2);
}

.brand-copy {
  min-width: 0;
}

.brand-title-row {
  gap: 0.55rem;
}

.header h1 {
  margin: 0;
  color: var(--text);
  font-size: clamp(1.15rem, 1.6vw, 1.45rem);
  font-weight: 760;
  letter-spacing: -0.025em;
}

.header p {
  margin: 0.18rem 0 0;
  overflow: hidden;
  color: var(--muted);
  font-size: 0.78rem;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.stage-badge {
  flex: 0 0 auto;
  padding: 0.16rem 0.45rem;
  border: 1px solid var(--primary-border);
  border-radius: 999px;
  background: var(--primary-soft);
  color: var(--primary);
  font-size: 0.66rem;
  font-weight: 700;
}

.theme-control {
  display: flex;
  flex: 0 0 auto;
  gap: 0.2rem;
  padding: 0.24rem;
  border: 1px solid var(--border);
  border-radius: 11px;
  background: var(--surface-muted);
}

.theme-option {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 0.34rem;
  min-height: 32px;
  padding: 0.35rem 0.58rem;
  border: 0;
  border-radius: 8px;
  background: transparent;
  color: var(--muted);
  cursor: pointer;
  font: inherit;
  font-size: 0.74rem;
  font-weight: 650;
  transition: 160ms ease;
}

.theme-option:hover {
  color: var(--text);
}

.theme-option.active {
  background: var(--surface);
  color: var(--primary);
  box-shadow: 0 2px 8px rgba(15, 23, 42, 0.1);
}

.theme-option:focus-visible,
button:focus-visible,
input:focus-visible,
textarea:focus-visible,
select:focus-visible {
  outline: none;
  box-shadow: var(--shadow-focus);
}

.main-content {
  display: grid;
  grid-template-columns: minmax(218px, 260px) minmax(480px, 1fr) minmax(285px, 340px);
  flex: 1;
  min-height: 0;
  gap: 0.75rem;
  padding: 0.75rem;
  overflow: hidden;
}

.column,
.col-control,
.col-chat,
.col-display {
  min-width: 0;
  min-height: 0;
}

.column {
  gap: 0.75rem;
}

.col-control {
  overflow-x: hidden;
  overflow-y: auto;
  scrollbar-width: thin;
  scrollbar-color: var(--border-strong) transparent;
}

.panel {
  border: 1px solid var(--border);
  border-radius: 13px;
  background: var(--surface);
  box-shadow: var(--shadow);
}

.col-chat .panel {
  border-color: var(--primary-border);
  box-shadow:
    var(--shadow),
    0 0 0 1px var(--primary-soft);
}

.panel-header,
.topology-toolbar {
  min-height: 43px;
  padding: 0.68rem 0.8rem;
  background: var(--surface-muted);
  border-bottom: 1px solid var(--border);
}

.panel-header h3,
.topology-toolbar .title {
  color: var(--text-soft);
  font-size: 0.84rem;
  font-weight: 720;
  letter-spacing: -0.01em;
}

.panel-icon {
  display: inline-grid;
  width: 1rem;
  place-items: center;
  color: var(--primary);
  font-family: ui-monospace, 'Cascadia Code', Consolas, monospace;
  font-weight: 800;
}

.panel-body {
  padding: 0.72rem;
}

.settings-card {
  max-height: 500px;
}

.code-textarea,
.form-select,
.form-input,
.chat-input {
  border: 1px solid var(--border-strong);
  border-radius: 8px;
  background: var(--surface-muted);
  color: var(--text);
  transition:
    border-color 160ms ease,
    box-shadow 160ms ease,
    background-color 160ms ease;
}

.code-textarea {
  min-height: 78px;
  padding: 0.7rem;
  font-size: 12px;
}

.code-textarea:focus,
.form-select:focus,
.form-input:focus,
.chat-input:focus {
  border-color: var(--primary);
  background: var(--surface);
  box-shadow: var(--shadow-focus);
}

.code-textarea::placeholder,
.form-input::placeholder,
.chat-input::placeholder {
  color: var(--subtle);
}

.form-group label,
.session-info .label {
  color: var(--muted);
}

.action-btn,
.chat-send-btn,
.modal-btn,
.retry-btn {
  border-radius: 8px;
  font-weight: 700;
  transition:
    background-color 160ms ease,
    transform 160ms ease,
    opacity 160ms ease;
}

.initial-btn,
.chat-send-btn {
  background: var(--primary);
  color: #ffffff;
}

.initial-btn:hover:not(:disabled),
.chat-send-btn:hover:not(:disabled) {
  background: var(--primary-hover);
  transform: translateY(-1px);
}

.session-card {
  background: var(--surface);
}

.session-id {
  max-width: 100%;
  overflow: hidden;
  background: var(--primary-soft);
  color: var(--primary);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.new-session-btn {
  background: var(--surface-strong);
  color: var(--text-soft);
}

.new-session-btn:hover {
  background: var(--primary-soft);
  color: var(--primary);
}

.chat-panel {
  min-height: 0;
}

.chat-body {
  padding: clamp(0.85rem, 1.4vw, 1.25rem);
  background:
    linear-gradient(var(--surface-muted), var(--surface-muted)) padding-box,
    var(--surface-muted);
  gap: 0.85rem;
}

.empty-chat {
  width: min(440px, 82%);
  margin: auto;
  padding: 1.2rem;
  border: 1px dashed var(--border-strong);
  border-radius: 12px;
  background: var(--surface);
  color: var(--muted);
  font-style: normal;
}

.chat-bubble {
  max-width: min(82%, 760px);
  padding: 0.78rem 0.9rem;
  border-radius: 12px;
  box-shadow: 0 4px 12px rgba(15, 23, 42, 0.06);
}

.chat-bubble.user {
  border-bottom-right-radius: 4px;
  background: var(--user-bubble);
}

.chat-bubble.agent {
  border: 1px solid var(--border);
  border-bottom-left-radius: 4px;
  background: var(--agent-bubble);
  color: var(--text);
}

.chat-bubble .avatar {
  color: var(--muted);
}

.chat-bubble.user .avatar {
  color: rgba(255, 255, 255, 0.8);
}

.bubble-markdown {
  color: inherit;
  font-size: 13.5px;
  line-height: 1.55;
  word-break: break-word;
}

.chat-footer {
  padding: 0.72rem;
  border-top: 1px solid var(--border);
  background: var(--surface);
}

.chat-input {
  min-width: 0;
  padding: 0.66rem 0.75rem;
}

.chat-send-btn {
  padding-inline: 1.1rem;
}

.chat-send-btn:disabled {
  background: var(--surface-strong);
  color: var(--subtle);
}

.tab-header {
  height: 43px;
  border: 1px solid var(--border);
  border-radius: 11px;
  background: var(--surface);
  box-shadow: var(--shadow);
}

.tab-btn {
  gap: 0.28rem;
  padding: 0.3rem 0.42rem;
  color: var(--muted);
  font-size: 0.72rem;
  font-weight: 650;
}

.tab-btn:hover {
  background: var(--surface-muted);
  color: var(--text);
}

.tab-btn.active {
  border-bottom: 2px solid var(--primary);
  background: var(--primary-soft);
  color: var(--primary);
}

.badge-neo4j {
  background: var(--accent-soft) !important;
  color: var(--accent);
  font-weight: 700;
}

.log-content {
  background: var(--terminal);
  color: var(--terminal-text);
}

.empty-logs,
.log-time {
  color: var(--terminal-muted);
}

.log-item {
  border-bottom-color: rgba(148, 163, 184, 0.14);
}

.log-item.info {
  color: var(--log-info);
}

.log-item.success {
  color: var(--log-success);
}

.log-item.error {
  color: var(--log-error);
}

.chat-bubble.coder {
  border-color: var(--coder-border);
  background: var(--coder-bg);
  color: var(--coder-text);
}

.chat-bubble.reviewer {
  border-color: var(--reviewer-border);
  background: var(--reviewer-bg);
  color: var(--reviewer-text);
}

.chat-bubble.architect {
  border-color: var(--architect-border);
  background: var(--architect-bg);
  color: var(--architect-text);
}

.chat-bubble.coder .avatar,
.chat-bubble.reviewer .avatar,
.chat-bubble.architect .avatar {
  color: inherit !important;
  opacity: 0.82;
}

.modal-overlay {
  background: var(--modal-overlay);
  backdrop-filter: blur(6px);
}

.modal-container {
  border-color: var(--primary-border);
  border-radius: 16px;
  background: var(--surface);
  box-shadow: 0 24px 70px rgba(2, 6, 23, 0.36);
}

.modal-header,
.modal-footer {
  border-color: var(--border);
  background: var(--surface-muted);
}

.modal-header h3 {
  color: var(--text);
}

.modal-tip {
  color: var(--muted);
}

.file-badge {
  border-color: var(--border-strong);
  background: var(--surface-strong);
  color: var(--warning);
}

.diff-panel {
  border-color: var(--border);
}

.diff-panel.original {
  background: var(--original-bg);
}

.diff-panel.modified {
  background: var(--modified-bg);
}

.diff-panel.original .diff-panel-title {
  background: var(--original-title);
  color: var(--danger);
}

.diff-panel.modified .diff-panel-title {
  background: var(--modified-title);
  color: var(--success);
}

.diff-pre {
  color: var(--text-soft);
}

:deep(.topology-container),
:deep(.vue-flow) {
  background: var(--surface-muted) !important;
}

:deep(.topology-container .topology-toolbar) {
  background: var(--surface-muted);
  border-color: var(--border);
}

:deep(.topology-container .topology-toolbar .title) {
  color: var(--text-soft);
}

:deep(.topology-container .refresh-btn) {
  background: var(--surface-strong);
  color: var(--text-soft);
}

:deep(.topology-container .refresh-btn:hover) {
  background: var(--primary-soft);
  color: var(--primary);
}

:deep(.dag-node-pending) {
  border-color: var(--border-strong) !important;
  background: var(--surface-strong) !important;
  color: var(--muted) !important;
}

:deep(.dag-node-in_progress) {
  border-color: var(--warning) !important;
  background: var(--surface) !important;
  color: var(--warning) !important;
}

:deep(.dag-node-completed) {
  border-color: var(--success) !important;
  background: var(--accent-soft) !important;
  color: var(--success) !important;
}

:deep(.dag-node-failed) {
  border-color: var(--danger) !important;
  background: var(--original-bg) !important;
  color: var(--danger) !important;
}

@media (max-width: 1080px) {
  :global(body) {
    overflow: auto;
  }

  .app-container {
    height: auto;
  }

  .main-content {
    grid-template-columns: minmax(220px, 250px) minmax(420px, 1fr);
    overflow: visible;
  }

  .col-control,
  .col-chat {
    min-height: 680px;
  }

  .col-display {
    grid-column: 1 / -1;
    min-height: 430px;
  }
}

@media (max-width: 720px) {
  .header {
    align-items: flex-start;
    flex-direction: column;
    padding: 0.75rem;
  }

  .theme-control {
    width: 100%;
  }

  .theme-option {
    flex: 1;
  }

  .main-content {
    display: flex;
    flex-direction: column;
    padding: 0.55rem;
  }

  .col-control,
  .col-chat,
  .col-display {
    min-height: 620px;
  }

  .col-display {
    min-height: 440px;
  }

  .diff-container {
    flex-direction: column;
    overflow-y: auto;
  }

  .modal-container {
    width: 94%;
    height: 90%;
  }
}

@media (prefers-reduced-motion: reduce) {
  *,
  *::before,
  *::after {
    scroll-behavior: auto !important;
    animation-duration: 0.01ms !important;
    animation-iteration-count: 1 !important;
    transition-duration: 0.01ms !important;
  }
}
</style>
