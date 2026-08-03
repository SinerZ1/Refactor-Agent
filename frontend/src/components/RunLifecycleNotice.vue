<script setup lang="ts">
import type { RunStatusSnapshot } from '../types/agentEvents'

defineProps<{ status: RunStatusSnapshot | null }>()
</script>

<template>
  <section v-if="status" class="run-lifecycle-notice" aria-live="polite">
    <p v-if="status.lifecycle_status === 'cleanup_pending'" class="notice warning">
      正在安全清理：后台运行仍在停止，隔离工作区会暂时保留以避免并发删除。
    </p>
    <p v-else-if="status.lifecycle_status === 'cleanup_completed'" class="notice success">
      延迟清理已完成，运行资源已安全释放。
    </p>
    <p v-else-if="status.lifecycle_status === 'cleanup_failed'" class="notice error">
      延迟清理失败，需要维护人员检查运行资源；请勿把它当作普通成功。
    </p>
    <p v-else-if="status.lifecycle_status === 'waiting_for_hitl'" class="notice info">
      正在等待审批；隔离工作区为可恢复 HITL 主动保留，并非资源泄漏。
    </p>

    <div
      v-if="status.apply_failure"
      class="apply-failure"
      :data-rollback="status.apply_failure.rollback_status"
    >
      <p v-if="status.apply_failure.rollback_status === 'partial'" class="notice error">
        应用未能完整恢复，系统没有覆盖第三方新修改。核对完成前不要重新运行应用。
      </p>
      <p v-else-if="status.apply_failure.rollback_status === 'complete'" class="notice warning">
        应用失败，但已完成补偿回滚；解决冲突后可以重新运行。
      </p>
      <p v-else class="notice warning">应用在写入前停止，真实源码未由本次事务改写。</p>
      <p>{{ status.apply_failure.guidance }}</p>
      <p v-if="status.apply_failure.affected_files.length">需要核对：</p>
      <ul v-if="status.apply_failure.affected_files.length">
        <li v-for="file in status.apply_failure.affected_files" :key="file">{{ file }}</li>
      </ul>
      <p v-if="status.apply_failure.recovery_available">
        恢复材料已保留（标识：{{ status.apply_failure.recovery_ids.join('、') }}）。
      </p>
    </div>
  </section>
</template>

<style scoped>
.run-lifecycle-notice {
  display: grid;
  gap: 0.5rem;
}
.notice {
  margin: 0;
  padding: 0.65rem 0.8rem;
  border-radius: 6px;
}
.info {
  background: color-mix(in srgb, #3b82f6 16%, transparent);
}
.warning {
  background: color-mix(in srgb, #f59e0b 18%, transparent);
}
.success {
  background: color-mix(in srgb, #10b981 16%, transparent);
}
.error {
  background: color-mix(in srgb, #ef4444 16%, transparent);
}
.apply-failure {
  padding: 0.75rem;
  border: 1px solid color-mix(in srgb, #ef4444 45%, transparent);
  border-radius: 6px;
}
.apply-failure ul {
  margin: 0.35rem 0;
  padding-left: 1.4rem;
}
</style>
