<script setup lang="ts">
const sourceCode = defineModel<string>({ required: true })

defineProps<{
  isRefactoring: boolean
  threadId: string
}>()

defineEmits<{
  newSession: []
  submit: []
}>()
</script>

<template>
  <div class="source-panels">
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
          class="action-btn initial-btn"
          :disabled="isRefactoring"
          @click="$emit('submit')"
        >
          <span v-if="isRefactoring" class="spinner"></span>
          {{ isRefactoring ? '分析重构中...' : '提交初始重构 👉' }}
        </button>
      </div>
    </div>

    <slot></slot>

    <div class="panel session-card">
      <div class="panel-header">
        <h3><span class="panel-icon" aria-hidden="true">◇</span> 会话记忆控制</h3>
      </div>
      <div class="panel-body">
        <div class="session-info">
          <span class="label">会话 ID:</span>
          <code class="session-id">{{ threadId }}</code>
        </div>
        <button class="action-btn new-session-btn" @click="$emit('newSession')">
          🔄 开启新会话 (重置记忆)
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.source-panels {
  display: contents;
}
</style>
