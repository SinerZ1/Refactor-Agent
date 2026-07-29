<script setup lang="ts">
import { nextTick, ref, watch } from 'vue'

import type { ChatMessage } from '../types/workspace'
import { renderSafeMarkdown } from '../utils/markdown'

const userInput = defineModel<string>({ required: true })
const props = defineProps<{
  isRefactoring: boolean
  messages: ChatMessage[]
}>()

defineEmits<{ send: [] }>()

const containerRef = ref<HTMLDivElement | null>(null)
watch(
  () => props.messages,
  () =>
    nextTick(() => {
      if (containerRef.value) containerRef.value.scrollTop = containerRef.value.scrollHeight
    }),
  { deep: true },
)
</script>

<template>
  <div class="column col-chat">
    <div class="panel chat-panel">
      <div class="panel-header">
        <h3><span class="panel-icon" aria-hidden="true">⌘</span> 多轮交互重构对话</h3>
      </div>
      <div ref="containerRef" class="chat-body">
        <div v-if="messages.length === 0" class="empty-chat">
          请先在左侧提交初始重构。重构完成后，你可以在此处连续对 Agent 发送追问（例如：“再重命名
          add 方法”、“写一个对应的单元测试”）。
        </div>
        <div
          v-for="(message, index) in messages"
          :key="index"
          :class="['chat-bubble', message.role]"
        >
          <div class="avatar">
            <span v-if="message.role === 'user'">👤 用户</span>
            <span v-else-if="message.role === 'agent'">🤖 Agent</span>
            <span v-else-if="message.role === 'coder'">🧑‍💻 CoderAgent (Developer)</span>
            <span v-else-if="message.role === 'reviewer'">🛡️ ReviewerAgent (Reviewer)</span>
            <span v-else-if="message.role === 'architect'">📐 ArchitectAgent (Architect)</span>
          </div>
          <div class="bubble-markdown" v-html="renderSafeMarkdown(message.text)"></div>
        </div>
      </div>
      <div class="chat-footer">
        <input
          v-model="userInput"
          :disabled="isRefactoring || messages.length === 0"
          type="text"
          placeholder="发送后续重构修改建议（例如: 优化代码结构、重命名变量等）..."
          class="chat-input"
          @keydown.enter="$emit('send')"
        />
        <button
          class="chat-send-btn"
          :disabled="isRefactoring || !userInput.trim() || messages.length === 0"
          @click="$emit('send')"
        >
          发送
        </button>
      </div>
    </div>
  </div>
</template>
