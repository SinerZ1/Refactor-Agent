<script setup lang="ts">
import { onMounted } from 'vue'

import { useModelProvider } from '../composables/useModelProvider'

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

onMounted(loadAdcStatus)

defineExpose({ getRuntimeModelConfig })
</script>

<template>
  <div class="panel settings-card">
    <div class="panel-header">
      <h3><span class="panel-icon" aria-hidden="true">⌘</span> 智能体模型配置</h3>
    </div>
    <div class="panel-body flex-column model-settings-body">
      <div class="form-group">
        <label>Provider:</label>
        <select v-model="modelConfig.provider" class="form-select" @change="handleProviderChange">
          <option value="openai">OpenAI API Compatible</option>
          <option value="gemini_studio">Gemini AI Studio</option>
          <option value="google_vertex">Google Vertex AI</option>
        </select>
      </div>

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
        <div class="form-group separated-field">
          <label>Base URL:</label>
          <input
            v-model="modelConfig.base_url"
            type="text"
            placeholder="https://api.deepseek.com"
            class="form-input"
            @input="saveConfig"
          />
        </div>
        <div class="form-group separated-field">
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
        <div class="form-group separated-field">
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

      <div v-if="modelConfig.provider === 'google_vertex'" class="provider-sub-form vertex-form">
        <div class="form-group">
          <label>Project ID:</label>
          <input
            v-model="modelConfig.vertex_project_id"
            type="password"
            placeholder="Google Cloud project ID"
            autocomplete="off"
            class="form-input"
            @input="saveConfig"
          />
        </div>
        <div class="form-group">
          <label>Location:</label>
          <input
            v-model="modelConfig.vertex_location"
            type="text"
            placeholder="global"
            class="form-input"
            @input="saveConfig"
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
            class="form-select"
            @change="handleProviderChange"
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
</template>

<style scoped>
.model-settings-body {
  gap: 0.8rem;
  overflow-y: auto;
}

.separated-field {
  margin-top: 0.4rem;
}

.vertex-form {
  display: flex;
  flex-direction: column;
  gap: 0.6rem;
}
</style>
