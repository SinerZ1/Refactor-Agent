<script setup lang="ts">
import { ref } from 'vue'

const sourceCode = ref('')
const refactoredCode = ref('')
const isRefactoring = ref(false)

const handleRefactor = async () => {
  if (!sourceCode.value.trim()) {
    alert('请输入需要重构的代码！')
    return
  }

  isRefactoring.value = true
  refactoredCode.value = '重构中，请稍候...'

  try {
    const response = await fetch('http://127.0.0.1:8000/api/refactor', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({ code: sourceCode.value })
    })

    if (!response.ok) {
      throw new Error(`HTTP error! status: ${response.status}`)
    }

    const data = await response.json()
    refactoredCode.value = data.refactored_code
  } catch (error) {
    console.error('Refactor API error:', error)
    refactoredCode.value = `# [重构失败]\n# 请确保后端服务已在 127.0.0.1:8000 运行\n# 错误信息: ${error}`
  } finally {
    isRefactoring.value = false
  }
}
</script>

<template>
  <div class="app-container">
    <header class="header">
      <h1>🚀 Refactor-Agent (阶段 1)</h1>
      <p>Python 极简代码重构助手</p>
    </header>

    <main class="main-content">
      <div class="editor-panel">
        <div class="panel-header">
          <h3>源代码 (Source Code)</h3>
        </div>
        <textarea
          v-model="sourceCode"
          class="code-textarea"
          placeholder="在此粘贴你需要重构的 Python 代码..."
        ></textarea>
      </div>

      <div class="action-panel">
        <button
          @click="handleRefactor"
          :disabled="isRefactoring"
          class="refactor-btn"
        >
          {{ isRefactoring ? '重构中...' : '开始重构 👉' }}
        </button>
      </div>

      <div class="editor-panel">
        <div class="panel-header">
          <h3>重构后 (Refactored)</h3>
        </div>
        <textarea
          v-model="refactoredCode"
          class="code-textarea result-textarea"
          readonly
          placeholder="重构后的代码将显示在这里..."
        ></textarea>
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

.result-textarea {
  background-color: #1a1a1a;
}

.action-panel {
  display: flex;
  align-items: center;
  justify-content: center;
}

.refactor-btn {
  padding: 1rem 2rem;
  font-size: 1.2rem;
  font-weight: bold;
  color: #fff;
  background-color: #4fc08d;
  border: none;
  border-radius: 8px;
  cursor: pointer;
  transition: background-color 0.2s;
  box-shadow: 0 4px 6px rgba(0, 0, 0, 0.3);
}

.refactor-btn:hover:not(:disabled) {
  background-color: #3aa876;
}

.refactor-btn:disabled {
  background-color: #555555;
  cursor: not-allowed;
  color: #888888;
}
</style>
