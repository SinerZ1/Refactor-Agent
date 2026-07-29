<script setup lang="ts">
import type { ThemePreference } from '../composables/useTheme'

interface ThemeOption {
  value: ThemePreference
  label: string
  icon: string
}

defineProps<{
  themeOptions: ThemeOption[]
  themePreference: ThemePreference
}>()

defineEmits<{
  selectTheme: [preference: ThemePreference]
}>()
</script>

<template>
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
        @click="$emit('selectTheme', option.value)"
      >
        <span aria-hidden="true">{{ option.icon }}</span>
        <span class="theme-option-label">{{ option.label }}</span>
      </button>
    </div>
  </header>
</template>
