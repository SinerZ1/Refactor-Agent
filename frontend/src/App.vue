<script setup lang="ts">
import { ref, computed, nextTick, watch } from 'vue'
import hljs from 'highlight.js'
import 'highlight.js/styles/vs2015.css' // 使用 VS2015 深色代码高亮主题

const sourceCode = ref('backend/CodeSmells/Calculator.py') // 默认填入要重构的测试文件路径
const refactoredCode = ref('')
const isRefactoring = ref(false)
const agentLogs = ref<{ type: 'info' | 'success' | 'error'; message: string }[]>([])

// 计算属性：利用 highlight.js 对生成的代码进行实时语法高亮
const highlightedCode = computed(() => {
  if (!refactoredCode.value) {
    return '<span style="color: #6a9955;"># 重构后的代码将在此显示...</span>'
  }
  try {
    return hljs.highlight(refactoredCode.value, { language: 'python' }).value
  } catch (error) {
    console.error('Highlight error:', error)
    return refactoredCode.value
  }
})

// 监听日志变化，自动滚动到日志区域底部
const logContainerRef = ref<HTMLDivElement | null>(null)
watch(agentLogs, () => {
  nextTick(() => {
    if (logContainerRef.value) {
      logContainerRef.value.scrollTop = logContainerRef.value.scrollHeight
    }
  })
}, { deep: true })

const handleRefactorStream = async () => {
  if (!sourceCode.value.trim()) {
    alert('请输入需要重构的代码内容或本地文件路径！')
    return
  }

  isRefactoring.value = true
  refactoredCode.value = '' 
  agentLogs.value = [] // 清空之前的日志

  try {
    const response = await fetch('http://127.0.0.1:8000/api/refactor/stream', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({ code: sourceCode.value })
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
              
              // 匹配阶段 3 的日志前缀，拦截并呈现在日志面板中
              if (token.startsWith('[INFO]')) {
                agentLogs.value.push({ type: 'info', message: token.replace('[INFO]', '').trim() })
              } else if (token.startsWith('[SUCCESS]')) {
                agentLogs.value.push({ type: 'success', message: token.replace('[SUCCESS]', '').trim() })
              } else if (token.startsWith('[ERROR]')) {
                agentLogs.value.push({ type: 'error', message: token.replace('[ERROR]', '').trim() })
              } else {
                // 如果不是日志，则是最终代码 Token，流入代码显示区域
                refactoredCode.value += token
              }
            }
          } catch (e) {
            console.error('解析 SSE 行失败:', line, e)
          }
        }
      }
    }
  } catch (error) {
    console.error('流式重构接口错误:', error)
    agentLogs.value.push({ type: 'error', message: `网络或接口调用异常: ${error}` })
  } finally {
    isRefactoring.value = false
  }
}
</script>

<template>
  <div class="app-container">
    <header class="header">
      <h1>🚀 Refactor-Agent (阶段 3)</h1>
      <p>Python 智能代码重构助手 —— 引入文件工具调用、测试自动运行 & 智能日志反馈</p>
    </header>

    <main class="main-content">
      <!-- 左侧：输入面板 -->
      <div class="editor-panel">
        <div class="panel-header">
          <h3>源代码或文件路径 (Source Code / File Path)</h3>
        </div>
        <textarea
          v-model="sourceCode"
          class="code-textarea"
          placeholder="在此输入需要重构的 Python 代码，或者输入要修改的本地路径（如: backend/CodeSmells/Calculator.py）"
        ></textarea>
      </div>

      <!-- 中间：控制按钮 -->
      <div class="action-panel">
        <button
          @click="handleRefactorStream"
          :disabled="isRefactoring"
          class="refactor-btn"
        >
          <span v-if="isRefactoring" class="spinner"></span>
          {{ isRefactoring ? '智能体运作中...' : '开始重构 👉' }}
        </button>
      </div>

      <!-- 右侧：包含日志面板和代码高亮面板 -->
      <div class="right-display-container">
        <!-- 右侧上部：智能体执行日志 -->
        <div class="panel-half logs-panel">
          <div class="panel-header">
            <h3>🛠️ Agent 思考与执行日志</h3>
          </div>
          <div ref="logContainerRef" class="log-content">
            <div v-if="agentLogs.length === 0" class="empty-logs">
              等待 Agent 执行。如果是本地路径，Agent 会自动：读取文件 -> 分析重构 -> 写回文件 -> 运行测试
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

        <!-- 右侧下部：重构后代码 -->
        <div class="panel-half code-panel">
          <div class="panel-header">
            <h3>📄 重构后结果 (Refactored Code)</h3>
          </div>
          <div class="code-viewer-container">
            <pre class="code-viewer"><code v-html="highlightedCode" class="hljs language-python"></code></pre>
          </div>
        </div>
      </div>
    </main>
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
  padding: 1rem 2rem;
  background-color: #2d2d2d;
  border-bottom: 1px solid #3d3d3d;
  text-align: center;
}

.header h1 {
  margin: 0;
  font-size: 1.8rem;
  color: #4fc08d;
}

.header p {
  margin: 0.5rem 0 0;
  font-size: 1rem;
  color: #9cdcfe;
}

.main-content {
  display: flex;
  flex: 1;
  padding: 1rem;
  gap: 1rem;
  overflow: hidden;
}

.editor-panel {
  flex: 1;
  display: flex;
  flex-direction: column;
  background-color: #252526;
  border: 1px solid #3d3d3d;
  border-radius: 8px;
  overflow: hidden;
}

.right-display-container {
  flex: 1.2;
  display: flex;
  flex-direction: column;
  gap: 1rem;
  height: 100%;
}

.panel-half {
  flex: 1;
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
  font-size: 1rem;
  color: #dcdcaa;
}

.code-textarea {
  flex: 1;
  width: 100%;
  padding: 1rem;
  background-color: #1e1e1e;
  color: #d4d4d4;
  border: none;
  resize: none;
  font-family: 'Fira Code', 'Courier New', Courier, monospace;
  font-size: 14px;
  line-height: 1.5;
  outline: none;
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
  color: #555;
  font-style: italic;
  text-align: center;
  margin-top: 2rem;
}

.log-item {
  margin-bottom: 0.6rem;
  padding-bottom: 0.6rem;
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
  padding: 1rem;
}

.code-viewer {
  margin: 0;
  background: transparent;
}

.code-viewer code {
  font-family: 'Fira Code', 'Courier New', Courier, monospace;
  font-size: 14px;
  line-height: 1.5;
  background: transparent;
  padding: 0;
  display: block;
  white-space: pre;
}

.action-panel {
  display: flex;
  align-items: center;
  justify-content: center;
}

.refactor-btn {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  padding: 1rem 1.8rem;
  font-size: 1.2rem;
  font-weight: bold;
  color: #fff;
  background-color: #4fc08d;
  border: none;
  border-radius: 8px;
  cursor: pointer;
  transition: background-color 0.2s, transform 0.1s;
  box-shadow: 0 4px 6px rgba(0, 0, 0, 0.3);
}

.refactor-btn:hover:not(:disabled) {
  background-color: #3aa876;
}

.refactor-btn:active:not(:disabled) {
  transform: scale(0.98);
}

.refactor-btn:disabled {
  background-color: #555555;
  cursor: not-allowed;
  color: #888888;
}

.spinner {
  width: 18px;
  height: 18px;
  border: 3px solid rgba(255, 255, 255, 0.3);
  border-top-color: #fff;
  border-radius: 50%;
  animation: spin 1s linear infinite;
}

@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}
</style>
