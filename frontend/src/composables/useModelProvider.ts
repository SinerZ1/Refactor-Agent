import { computed, ref } from 'vue'

import { API_BASE_URL } from '../api'

export type ModelProvider = 'openai' | 'gemini_studio' | 'google_vertex'
export type VertexAuthMode = 'adc' | 'api_key'

export interface ModelConfig {
  provider: ModelProvider
  base_url: string
  model_name: string
  vertex_project_id: string
  vertex_location: string
  vertex_model_name: string
  vertex_auth_mode: VertexAuthMode
}

interface ConnectionFeedback {
  type: 'idle' | 'success' | 'error'
  message: string
}

interface AdcStatus {
  available: boolean
  message: string
  source?: string | null
  project_id?: string | null
  credential_type?: string | null
}

const MODEL_CONFIG_STORAGE_KEY = 'refactor_agent_model_config'

export function useModelProvider() {
  const modelConfig = ref<ModelConfig>({
    provider: 'openai',
    base_url: 'https://api.deepseek.com',
    model_name: 'deepseek-v4-flash',
    vertex_project_id: '',
    vertex_location: 'global',
    vertex_model_name: 'gemini-3.5-flash',
    vertex_auth_mode: 'adc',
  })

  // API Key 按供应商隔离且仅保存在内存中，避免跨供应商误发或落入 LocalStorage。
  const providerApiKeys = ref<Record<ModelProvider, string>>({
    openai: '',
    gemini_studio: '',
    google_vertex: '',
  })
  const modelOptions = ref<Record<ModelProvider, string[]>>({
    openai: [],
    gemini_studio: [],
    google_vertex: [],
  })
  const isConnectingProvider = ref(false)
  const connectionFeedback = ref<ConnectionFeedback>({ type: 'idle', message: '' })
  const adcStatus = ref<AdcStatus>({ available: false, message: '正在检查 ADC…' })

  const saveConfig = () => {
    localStorage.setItem(MODEL_CONFIG_STORAGE_KEY, JSON.stringify(modelConfig.value))
  }

  const loadSavedConfig = () => {
    const saved = localStorage.getItem(MODEL_CONFIG_STORAGE_KEY)
    if (!saved) return
    try {
      const parsed = JSON.parse(saved) as Partial<ModelConfig> & Record<string, unknown>
      // 兼容旧版本并主动清理曾落盘的密钥和本机凭据路径。
      delete parsed.api_key
      delete parsed.vertex_adc_path
      modelConfig.value = { ...modelConfig.value, ...parsed }
    } catch (error) {
      console.error('加载本地模型配置失败:', error)
    }
  }

  const currentApiKey = computed({
    get: () => providerApiKeys.value[modelConfig.value.provider],
    set: (value: string) => {
      providerApiKeys.value[modelConfig.value.provider] = value
    },
  })

  const activeModelName = computed({
    get: () =>
      modelConfig.value.provider === 'google_vertex'
        ? modelConfig.value.vertex_model_name
        : modelConfig.value.model_name,
    set: (value: string) => {
      if (modelConfig.value.provider === 'google_vertex') {
        modelConfig.value.vertex_model_name = value
      } else {
        modelConfig.value.model_name = value
      }
      saveConfig()
    },
  })

  const activeModelOptions = computed(() => modelOptions.value[modelConfig.value.provider])
  const canConnectProvider = computed(() => {
    if (modelConfig.value.provider === 'openai') {
      return Boolean(currentApiKey.value.trim() && modelConfig.value.base_url.trim())
    }
    if (modelConfig.value.provider === 'gemini_studio') {
      return Boolean(currentApiKey.value.trim())
    }
    if (modelConfig.value.vertex_auth_mode === 'api_key') {
      return Boolean(currentApiKey.value.trim())
    }
    return adcStatus.value.available
  })

  const loadAdcStatus = async () => {
    try {
      const response = await fetch(`${API_BASE_URL}/api/models/adc-status`)
      if (!response.ok) throw new Error(`HTTP ${response.status}`)
      adcStatus.value = (await response.json()) as AdcStatus
      if (!modelConfig.value.vertex_project_id && adcStatus.value.project_id) {
        modelConfig.value.vertex_project_id = adcStatus.value.project_id
        saveConfig()
      }
    } catch {
      adcStatus.value = { available: false, message: '无法检查 ADC，请确认后端已启动' }
    }
  }

  const handleProviderChange = () => {
    connectionFeedback.value = { type: 'idle', message: '' }
    saveConfig()
    if (modelConfig.value.provider === 'google_vertex') {
      void loadAdcStatus()
    }
  }

  const getRuntimeModelConfig = () => ({
    ...modelConfig.value,
    api_key: currentApiKey.value,
  })

  const connectModelProvider = async () => {
    isConnectingProvider.value = true
    connectionFeedback.value = { type: 'idle', message: '' }
    try {
      const response = await fetch(`${API_BASE_URL}/api/models/connect`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          provider: modelConfig.value.provider,
          api_key: currentApiKey.value,
          base_url: modelConfig.value.base_url,
          project_id: modelConfig.value.vertex_project_id,
          location: modelConfig.value.vertex_location,
          auth_mode: modelConfig.value.vertex_auth_mode,
        }),
      })
      const payload = (await response.json()) as {
        models?: string[]
        message?: string
        detail?: string
      }
      if (!response.ok) {
        throw new Error(payload.detail || `连接失败（HTTP ${response.status}）`)
      }

      const models = payload.models ?? []
      modelOptions.value[modelConfig.value.provider] = models
      if (models.length > 0 && !models.includes(activeModelName.value)) {
        activeModelName.value = models[0]!
      }
      connectionFeedback.value = {
        type: 'success',
        message: payload.message || `连接成功，共发现 ${models.length} 个模型`,
      }
    } catch (error) {
      connectionFeedback.value = {
        type: 'error',
        message: error instanceof Error ? error.message : '连接失败，请检查配置',
      }
    } finally {
      isConnectingProvider.value = false
    }
  }

  loadSavedConfig()

  return {
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
  }
}
