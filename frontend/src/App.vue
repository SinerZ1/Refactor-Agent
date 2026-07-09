<script setup lang="ts">
import { ref, computed, nextTick, watch, onMounted } from 'vue'
import hljs from 'highlight.js'
import 'highlight.js/styles/vs2015.css' // 使用 VS2015 深色代码高亮主题
import TopologyGraph from './components/TopologyGraph.vue'

// 基础状态
const sourceCode = ref('CodeSmells/main.py') // 默认要重构的测试文件路径
const refactoredCode = ref('')
const isRefactoring = ref(false)
const agentLogs = ref<{ type: 'info' | 'success' | 'error'; message: string }[]>([])

// 阶段 4：会话与多轮对话记忆状态
const threadId = ref('session_' + Math.random().toString(36).substring(2, 9))
const userChatInput = ref('')
const chatMessages = ref<{ role: 'user' | 'agent' | 'coder' | 'reviewer' | 'architect'; text: string }[]>([])

// 阶段 1：多模型与服务提供商动态配置状态
const modelConfig = ref({
  provider: 'openai',
  api_key: '',
  base_url: 'https://api.siliconflow.cn/v1',
  model_name: 'deepseek-ai/DeepSeek-V4-Flash',
  vertex_project_id: 'project-c756c615-f8ff-41ea-8a3',
  vertex_location: 'us-central1',
  vertex_model_name: 'gemini-3.5-flash',
  vertex_auth_mode: 'adc', // 'adc' | 'api_key'
  vertex_adc_path: 'C:\\Users\\10900\\AppData\\Roaming\\gcloud\\application_default_credentials.json'
})

// 从 LocalStorage 加载本地模型配置
const loadSavedConfig = () => {
  const saved = localStorage.getItem('refactor_agent_model_config')
  if (saved) {
    try {
      const parsed = JSON.parse(saved)
      modelConfig.value = { ...modelConfig.value, ...parsed }
    } catch (e) {
      console.error('加载本地模型配置失败:', e)
    }
  }
}

// 保存配置到 LocalStorage
const saveConfig = () => {
  localStorage.setItem('refactor_agent_model_config', JSON.stringify(modelConfig.value))
}

// 页面加载时载入配置
loadSavedConfig()

// 阶段 2：WebSocket 双向全双工 & 人机协作审批 (HITL)
const socket = ref<WebSocket | null>(null)
const isApprovalModalOpen = ref(false)
const approvalPayload = ref<{
  file_path: string
  original_code: string
  refactored_code: string
} | null>(null)

// 初始化 WebSocket 连接并监听
const initWebSocket = () => {
  if (socket.value) {
    socket.value.close()
  }

  const wsUrl = `ws://127.0.0.1:8000/ws/refactor/${threadId.value}`
  console.log(`[WebSocket] Connecting to ${wsUrl}`)
  
  socket.value = new WebSocket(wsUrl)

  socket.value.onmessage = (event) => {
    try {
      const data = JSON.parse(event.data)
      console.log('[WebSocket] Received message:', data)

      if (data.type === 'approval_request') {
        // 挂起状态，显示 HITL 审批弹窗
        approvalPayload.value = data.payload
        isApprovalModalOpen.value = true
      } else if (data.type === 'approval_confirmed') {
        // 审批结果确认，隐藏弹窗，发起新的 SSE 重构流请求进行恢复
        isApprovalModalOpen.value = false
        sendStreamRequest('', false)
      } else if (data.type === 'chatroom_message') {
        // 阶段 3：A2A 多角色群聊，分发消息角色与头像
        const sender = data.sender
        const content = data.content
        let role: 'coder' | 'reviewer' | 'architect' = 'coder'
        if (sender === 'ReviewerAgent') role = 'reviewer'
        else if (sender === 'ArchitectAgent') role = 'architect'
        
        chatMessages.value.push({
          role: role,
          text: content
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

// 批准写入修改
const handleApprove = () => {
  if (socket.value && socket.value.readyState === WebSocket.OPEN) {
    socket.value.send(JSON.stringify({
      type: 'approval_response',
      approved: true
    }))
  }
}

// 拒绝写入修改并打回
const handleReject = () => {
  if (socket.value && socket.value.readyState === WebSocket.OPEN) {
    socket.value.send(JSON.stringify({
      type: 'approval_response',
      approved: false
    }))
    isApprovalModalOpen.value = false
  }
}

// 挂载时启动
onMounted(() => {
  initWebSocket()
})

// 阶段 6: 视图切换和拓扑组件引用
const activeTab = ref<'code' | 'topology'>('code')
const topologyGraphRef = ref<InstanceType<typeof TopologyGraph> | null>(null)

// 计算属性：利用 highlight.js 对生成的代码进行实时语法高亮
const highlightedCode = computed(() => {
  if (!refactoredCode.value) {
    return '<span style="color: #6a9955;"># 重构后的最新代码将在此显示...</span>'
  }
  try {
    return hljs.highlight(refactoredCode.value, { language: 'python' }).value
  } catch (error) {
    console.error('Highlight error:', error)
    return refactoredCode.value
  }
})

// 自动滚动控制
const logContainerRef = ref<HTMLDivElement | null>(null)
const chatContainerRef = ref<HTMLDivElement | null>(null)

watch(agentLogs, () => {
  nextTick(() => {
    if (logContainerRef.value) {
      logContainerRef.value.scrollTop = logContainerRef.value.scrollHeight
    }
  })
}, { deep: true })

watch(chatMessages, () => {
  nextTick(() => {
    if (chatContainerRef.value) {
      chatContainerRef.value.scrollTop = chatContainerRef.value.scrollHeight
    }
  })
}, { deep: true })

// 重置会话 (New Session)
const handleNewSession = () => {
  threadId.value = 'session_' + Math.random().toString(36).substring(2, 9)
  refactoredCode.value = ''
  userChatInput.value = ''
  chatMessages.value = []
  agentLogs.value = []
  initWebSocket()
}

// 核心流式请求方法
const sendStreamRequest = async (payloadText: string, isInitialTurn: boolean) => {
  isRefactoring.value = true
  if (isInitialTurn) {
    refactoredCode.value = ''
    chatMessages.value = []
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
    const response = await fetch('http://127.0.0.1:8000/api/refactor/stream', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({ 
        code: payloadText,
        thread_id: threadId.value,
        model_config: modelConfig.value
      })
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
            const parsed = JSON.parse(jsonStr)
            if (parsed.token) {
              const token = parsed.token
              
              if (token.startsWith('[INFO]')) {
                agentLogs.value.push({ type: 'info', message: token.replace('[INFO]', '').trim() })
              } else if (token.startsWith('[SUCCESS]')) {
                agentLogs.value.push({ type: 'success', message: token.replace('[SUCCESS]', '').trim() })
              } else if (token.startsWith('[ERROR]')) {
                agentLogs.value.push({ type: 'error', message: token.replace('[ERROR]', '').trim() })
              } else {
                // 累积代码文本并更新视图
                accumulatedResponse += token
                refactoredCode.value = accumulatedResponse
                if (chatMessages.value[agentMessageIndex]) {
                  chatMessages.value[agentMessageIndex].text = accumulatedResponse
                }
              }
            }
          } catch (e) {
            console.error('解析 SSE 失败:', line, e)
          }
        }
      }
    }
  } catch (error) {
    console.error('SSE Error:', error)
    agentLogs.value.push({ type: 'error', message: `错误: ${error}` })
    if (chatMessages.value[agentMessageIndex]) {
      chatMessages.value[agentMessageIndex].text = `[重构失败] 无法完成此次对话，请检查后端运行状态。`
    }
  } finally {
    isRefactoring.value = false
    // 流程结束后，自动刷新图谱以展示最新架构关系
    nextTick(() => {
      topologyGraphRef.value?.refresh()
    })
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
  <div class="app-container">
    <header class="header">
      <h1>🚀 Refactor-Agent (阶段 6)</h1>
      <p>Multi-Agent 协同分布式架构重构智能体 —— LangGraph & Neo4j 依赖拓扑协同可视化</p>
    </header>

    <main class="main-content">
      <!-- 栏 1：控制与初始输入 -->
      <div class="column col-control">
        <div class="panel">
          <div class="panel-header">
            <h3>⚙️ 初始代码/本地文件</h3>
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
            <h3>⚙️ 智能体模型配置</h3>
          </div>
          <div class="panel-body flex-column" style="gap: 0.8rem; overflow-y: auto;">
            <div class="form-group">
              <label>服务提供商 (Provider):</label>
              <select v-model="modelConfig.provider" @change="saveConfig" class="form-select">
                <option value="openai">OpenAI 兼容 / 智谱 / 国产模型</option>
                <option value="gemini_studio">Gemini AI Studio</option>
                <option value="google_vertex">Google Vertex AI</option>
              </select>
            </div>

            <!-- OpenAI 兼容配置 -->
            <div v-if="modelConfig.provider === 'openai'" class="provider-sub-form">
              <div class="form-group">
                <label>API Key:</label>
                <input v-model="modelConfig.api_key" @input="saveConfig" type="password" placeholder="请输入 API Key" class="form-input" />
              </div>
              <div class="form-group" style="margin-top: 0.4rem;">
                <label>Base URL:</label>
                <input v-model="modelConfig.base_url" @input="saveConfig" type="text" placeholder="https://api.openai.com/v1" class="form-input" />
              </div>
              <div class="form-group" style="margin-top: 0.4rem;">
                <label>模型名称 (Model):</label>
                <input v-model="modelConfig.model_name" @input="saveConfig" type="text" placeholder="gpt-4o-mini" class="form-input" />
              </div>
            </div>

            <!-- Gemini AI Studio 配置 -->
            <div v-if="modelConfig.provider === 'gemini_studio'" class="provider-sub-form">
              <div class="form-group">
                <label>Gemini API Key:</label>
                <input v-model="modelConfig.api_key" @input="saveConfig" type="password" placeholder="请输入 Gemini API Key" class="form-input" />
              </div>
              <div class="form-group" style="margin-top: 0.4rem;">
                <label>模型名称 (Model):</label>
                <input v-model="modelConfig.model_name" @input="saveConfig" type="text" placeholder="gemini-1.5-flash" class="form-input" />
              </div>
            </div>

            <!-- Google Vertex AI 配置 -->
            <div v-if="modelConfig.provider === 'google_vertex'" class="provider-sub-form" style="display: flex; flex-direction: column; gap: 0.6rem;">
              <div class="form-group">
                <label>Project ID (项目ID):</label>
                <input v-model="modelConfig.vertex_project_id" @input="saveConfig" type="text" placeholder="GCP 项目 ID" class="form-input" />
              </div>
              <div class="form-group">
                <label>Location (可用区):</label>
                <input v-model="modelConfig.vertex_location" @input="saveConfig" type="text" placeholder="us-central1" class="form-input" />
              </div>
              <div class="form-group">
                <label>Vertex 模型名称:</label>
                <input v-model="modelConfig.vertex_model_name" @input="saveConfig" type="text" placeholder="gemini-3.5-flash" class="form-input" />
              </div>
              <div class="form-group">
                <label>验证方式 (Auth Mode):</label>
                <select v-model="modelConfig.vertex_auth_mode" @change="saveConfig" class="form-select">
                  <option value="adc">本地 ADC 凭证路径 (推荐)</option>
                  <option value="api_key">Vertex API KEY 验证</option>
                </select>
              </div>
              <div v-if="modelConfig.vertex_auth_mode === 'adc'" class="form-group">
                <label>ADC JSON 凭据路径:</label>
                <input v-model="modelConfig.vertex_adc_path" @input="saveConfig" type="text" placeholder="本地 JSON 凭据绝对路径" class="form-input" />
              </div>
              <div v-if="modelConfig.vertex_auth_mode === 'api_key'" class="form-group">
                <label>Vertex API Key:</label>
                <input v-model="modelConfig.api_key" @input="saveConfig" type="password" placeholder="请输入 API Key" class="form-input" />
              </div>
            </div>
          </div>
        </div>

        <!-- 阶段 4 记忆卡片 -->
        <div class="panel session-card">
          <div class="panel-header">
            <h3>💾 会话记忆控制</h3>
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
            <h3>💬 多轮交互重构对话</h3>
          </div>
          
          <div ref="chatContainerRef" class="chat-body">
            <div v-if="chatMessages.length === 0" class="empty-chat">
              请先在左侧提交初始重构。重构完成后，你可以在此处连续对 Agent 发送追问（例如：“再重命名 add 方法”、“写一个对应的单元测试”）。
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
              <pre class="bubble-text">{{ msg.text }}</pre>
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
            📄 代码视图 (日志与源码)
          </button>
          <button 
            :class="['tab-btn', activeTab === 'topology' ? 'active' : '']" 
            @click="activeTab = 'topology'"
          >
            🕸️ 架构调用依赖拓扑图谱
          </button>
        </div>

        <!-- 3-A: 代码与日志视图 -->
        <div v-show="activeTab === 'code'" class="tab-content flex-column" style="gap: 0.8rem; height: calc(100% - 44px);">
          <!-- 3-1: 运行日志 -->
          <div class="panel display-half">
            <div class="panel-header">
              <h3>🛠️ Agent 思考与工具调用日志</h3>
            </div>
            <div ref="logContainerRef" class="log-content">
              <div v-if="agentLogs.length === 0" class="empty-logs">
                等待 Agent 执行操作...
              </div>
              <div
                v-for="(log, idx) in agentLogs"
                :key="idx"
                :class="['log-item', log.type]"
              >
                <span class="log-time">[{{ new Date().toLocaleTimeString() }}]</span>
                <pre class="log-message">{{ log.message }}</pre>
              </div>
            </div>
          </div>

          <!-- 3-2: 最新高亮代码 -->
          <div class="panel display-half">
            <div class="panel-header">
              <h3>📄 重构后最新完整代码</h3>
            </div>
            <div class="code-viewer-container">
              <pre class="code-viewer"><code v-html="highlightedCode" class="hljs language-python"></code></pre>
            </div>
          </div>
        </div>

        <!-- 3-B: 拓扑图谱视图 -->
        <div v-show="activeTab === 'topology'" class="tab-content" style="height: calc(100% - 44px);">
          <div class="panel" style="height: 100%;">
            <TopologyGraph ref="topologyGraphRef" />
          </div>
        </div>
      </div>
    </main>

    <!-- 阶段 2：人机协作审批 (HITL) 弹窗 -->
    <div v-if="isApprovalModalOpen && approvalPayload" class="modal-overlay">
      <div class="modal-container">
        <div class="modal-header">
          <h3>🛡️ 人机协作审批 (HITL) —— 代码修改确认</h3>
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
              <pre class="diff-pre"><code>{{ approvalPayload.original_code || '# 这是一个新建的文件，原代码为空。' }}</code></pre>
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

.bubble-text {
  margin: 0;
  white-space: pre-wrap;
  word-break: break-all;
  font-family: inherit;
  font-size: 13.5px;
  line-height: 1.4;
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

/* 展示栏专属 */
.display-half {
  flex: 1;
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

/* 代码区域样式 */
.code-viewer-container {
  flex: 1;
  background-color: #1e1e1e;
  overflow: auto;
  padding: 0.8rem;
}

.code-viewer {
  margin: 0;
  background: transparent;
}

.code-viewer code {
  font-family: 'Fira Code', 'Courier New', Courier, monospace;
  font-size: 13px;
  line-height: 1.4;
  background: transparent;
  padding: 0;
  display: block;
  white-space: pre;
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
  to { transform: rotate(360deg); }
}

@keyframes fadeIn {
  from { opacity: 0; transform: translateY(5px); }
  to { opacity: 1; transform: translateY(0); }
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

.form-select, .form-input {
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

.form-select:focus, .form-input:focus {
  border-color: #4fc08d;
  box-shadow: 0 0 3px rgba(79, 192, 141, 0.4);
}

.provider-sub-form {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
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
  from { transform: scale(0.95); opacity: 0; }
  to { transform: scale(1); opacity: 1; }
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
</style>
