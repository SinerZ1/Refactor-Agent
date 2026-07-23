const configuredApiBase = import.meta.env.VITE_API_BASE_URL?.trim()

// 开发服务器仍代理到默认后端；桌面构建产物由 FastAPI 同源托管，因此自动跟随
// Uvicorn 动态端口。这个边界让组件不再各自硬编码网络拓扑。
export const API_BASE_URL =
  configuredApiBase ||
  (window.location.port === '5173' ? 'http://127.0.0.1:8000' : window.location.origin)
