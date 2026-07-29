<script setup lang="ts">
import type { ApprovalPayload } from '../types/workspace'

defineProps<{
  open: boolean
  payload: ApprovalPayload | null
}>()

defineEmits<{
  approve: []
  reject: []
}>()
</script>

<template>
  <div v-if="open && payload" class="modal-overlay">
    <div class="modal-container">
      <div class="modal-header">
        <h3>
          <span class="panel-icon" aria-hidden="true">◇</span> 人机协作审批 (HITL) —— 代码修改确认
        </h3>
        <span class="file-badge">{{ payload.file_path }}</span>
      </div>
      <div class="modal-body">
        <p class="modal-tip">
          Developer Agent 申请写入文件。为了系统的安全和质量，请审查以下原代码与重构代码的对比。
        </p>
        <div class="diff-container">
          <div class="diff-panel original">
            <div class="diff-panel-title">原代码 (Original)</div>
            <pre
              class="diff-pre"
            ><code>{{ payload.original_code || '# 这是一个新建的文件，原代码为空。' }}</code></pre>
          </div>
          <div class="diff-panel modified">
            <div class="diff-panel-title">重构代码 (Refactored)</div>
            <pre class="diff-pre"><code>{{ payload.refactored_code }}</code></pre>
          </div>
        </div>
      </div>
      <div class="modal-footer">
        <button class="modal-btn btn-reject" @click="$emit('reject')">❌ 拒绝并退回</button>
        <button class="modal-btn btn-approve" @click="$emit('approve')">🟢 批准写入放行</button>
      </div>
    </div>
  </div>
</template>
