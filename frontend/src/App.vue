<script setup lang="ts">
import { ref, computed } from 'vue'
import hljs from 'highlight.js'
import 'highlight.js/styles/vs2015.css' // 使用 VS2015 深色代码高亮主题

const sourceCode = ref('')
const refactoredCode = ref('')
const isRefactoring = ref(false)

// 计算属性：利用 highlight.js 对生成的代码进行实时语法高亮
const highlightedCode = computed(() => {
  if (!refactoredCode.value) {
    return '<span style="color: #6a9955;"># 重构后的代码将显示在这里...</span>'
  }
  try {
    return hljs.highlight(refactoredCode.value, { language: 'python' }).value
  } catch (error) {
    console.error('Highlight error:', error)
    return refactoredCode.value
  }
})

const handleRefactorStream = async () => {
  if (!sourceCode.value.trim()) {
    alert('请输入需要重构的代码！')
    return
  }

  isRefactoring.value = true
  refactoredCode.value = '' // 清空之前的内容，准备流式写入

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

      // 解码当前数据块并拼接到缓存区
      buffer += decoder.decode(value, { stream: true })
      
      // 按 SSE 的双换行符分割事件
      const lines = buffer.split('\n\n')
      // 最后一个元素可能是未接收完整的行，留给下一次拼接
      buffer = lines.pop() || ''

      for (const line of lines) {
        if (line.trim().startsWith('data: ')) {
          try {
            const jsonStr = line.replace(/^data:\s*/, '')
            const parsed = JSON.parse(jsonStr)
            if (parsed.token) {
              // 实时追加 Token，触发 Vue 的响应式更新和计算属性高亮
              refactoredCode.value += parsed.token
            }
          } catch (e) {
            console.error('解析 SSE 行失败:', line, e)
          }
        }
      }
    }
  } catch (error) {
    console.error('流式重构接口错误:', error)
    refactoredCode.value = `# [重构失败]\n# 请确保后端服务已在 127.0.0.1:8000 运行\n# 错误信息: ${error}`
  } finally {
    isRefactoring.value = false
  }
}
</script>

<template>
  <div class="app-container">
    <header class="header">
      <h1>🚀 Refactor-Agent (阶段 2)</h1>
      <p>Python 智能代码重构助手 —— 实时流式输出 & 代码语法高亮</p>
    </header>

    <main class="main-content">
      <!-- 左侧：源代码输入 -->
      <div class="editor-panel">
        <div class="panel-header">
          <h3>源代码 (Source Code)</h3>
        </div>
        <textarea
          v-model="sourceCode"
          class="code-textarea"
          placeholder="在此粘贴或输入需要重构的 Python 代码..."
        ></textarea>
      </div>

      <!-- 中间：重构动作控制 -->
      <div class="action-panel">
        <button
          @click="handleRefactorStream"
          :disabled="isRefactoring"
          class="refactor-btn"
        >
          <span v-if="isRefactoring" class="spinner"></span>
          {{ isRefactoring ? '流式重构中...' : '开始重构 👉' }}
        </button>
      </div>

      <!-- 右侧：重构结果展示 (高亮) -->
      <div class="editor-panel">
        <div class="panel-header">
          <h3>重构后 (Refactored)</h3>
        </div>
        <div class="code-viewer-container">
          <pre class="code-viewer"><code v-html="highlightedCode" class="hljs language-python"></code></pre>
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

.panel-header {
  padding: 0.8rem;
  background-color: #333333;
  border-bottom: 1px solid #3d3d3d;
}

.panel-header h3 {
  margin: 0;
  font-size: 1.1rem;
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

/* 语法高亮预览面板样式 */
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

/* Loading 旋转动画 */
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
