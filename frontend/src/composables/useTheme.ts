import { computed, onMounted, onUnmounted, ref, watch } from 'vue'

export type ThemePreference = 'light' | 'dark' | 'system'

const THEME_STORAGE_KEY = 'refactor_agent_theme'

export function useTheme() {
  const storedTheme = localStorage.getItem(THEME_STORAGE_KEY)
  const themePreference = ref<ThemePreference>(
    storedTheme === 'light' || storedTheme === 'dark' || storedTheme === 'system'
      ? storedTheme
      : 'system',
  )
  const systemPrefersDark = ref(false)
  const resolvedTheme = computed<'light' | 'dark'>(() =>
    themePreference.value === 'system'
      ? systemPrefersDark.value
        ? 'dark'
        : 'light'
      : themePreference.value,
  )
  const themeOptions: { value: ThemePreference; label: string; icon: string }[] = [
    { value: 'light', label: '浅色', icon: '☀' },
    { value: 'dark', label: '深色', icon: '☾' },
    { value: 'system', label: '跟随系统', icon: '◐' },
  ]

  let systemThemeQuery: MediaQueryList | null = null

  const syncSystemTheme = (event?: MediaQueryListEvent) => {
    systemPrefersDark.value = event?.matches ?? systemThemeQuery?.matches ?? false
  }

  const setThemePreference = (theme: ThemePreference) => {
    themePreference.value = theme
  }

  watch(themePreference, (theme) => {
    localStorage.setItem(THEME_STORAGE_KEY, theme)
  })

  onMounted(() => {
    if (typeof window.matchMedia !== 'function') return
    systemThemeQuery = window.matchMedia('(prefers-color-scheme: dark)')
    syncSystemTheme()
    systemThemeQuery.addEventListener('change', syncSystemTheme)
  })

  onUnmounted(() => {
    systemThemeQuery?.removeEventListener('change', syncSystemTheme)
  })

  return {
    resolvedTheme,
    setThemePreference,
    themeOptions,
    themePreference,
  }
}
