import DOMPurify from 'dompurify'
import { Marked } from 'marked'

const marked = new Marked({
  gfm: true,
  breaks: true,
})

const SAFE_URI_PATTERN = /^(?:(?:https?|mailto|tel):|(?:[/?#.]|[^a-z]|[a-z+.-]+(?:[^a-z+.\-:]|$)))/i
const EXTERNAL_LINK_REL = ['noopener', 'noreferrer']

/**
 * Markdown 是 Agent 输出与可执行 DOM 之间的唯一信任边界。
 *
 * 这里保留 Marked 的 GFM 能力，再以 DOMPurify 建立 allowlist，而不是在各消息
 * 通道分别转义。这样 SSE、WebSocket 或未来新增的模型来源只要进入统一消息视图，
 * 都会遵守同一份安全策略，避免出现某条状态流绕过清洗的“第二渲染路径”。
 */
export const renderSafeMarkdown = (source: string): string => {
  if (!source) return ''

  const parsed = marked.parse(source) as string
  const sanitized = DOMPurify.sanitize(parsed, {
    USE_PROFILES: { html: true },
    FORBID_TAGS: ['script', 'iframe', 'object', 'embed'],
    ALLOW_DATA_ATTR: false,
    ALLOW_UNKNOWN_PROTOCOLS: false,
    ALLOWED_URI_REGEXP: SAFE_URI_PATTERN,
  })

  // `rel` 是链接的浏览器能力约束，不应依赖模型是否主动生成。DOMPurify 先
  // 移除危险 URL，再对仍被允许的跨源 HTTP(S) 链接补齐隔离语义。
  const document = new DOMParser().parseFromString(sanitized, 'text/html')
  for (const link of document.querySelectorAll('a[href]')) {
    const href = link.getAttribute('href')
    if (!href) continue

    try {
      const target = new URL(href, window.location.href)
      if (
        (target.protocol === 'http:' || target.protocol === 'https:') &&
        target.origin !== window.location.origin
      ) {
        const rel = new Set((link.getAttribute('rel') ?? '').split(/\s+/).filter(Boolean))
        for (const value of EXTERNAL_LINK_REL) rel.add(value)
        link.setAttribute('rel', [...rel].join(' '))
      }
    } catch {
      // 畸形 URL 属于不可信模型输出；移除导航能力比让整条消息渲染失败更安全。
      link.removeAttribute('href')
    }
  }

  return document.body.innerHTML
}
