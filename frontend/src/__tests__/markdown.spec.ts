import { describe, expect, it } from 'vitest'
import { renderSafeMarkdown } from '../utils/markdown'

const render = (markdown: string) => {
  const container = document.createElement('div')
  container.innerHTML = renderSafeMarkdown(markdown)
  return container
}

describe('renderSafeMarkdown', () => {
  it('renders GFM text, tables, and safe external links', () => {
    const output = render(
      [
        '**重构建议**',
        '',
        '| 文件 | 状态 |',
        '| --- | --- |',
        '| app.py | 完成 |',
        '',
        '[文档](https://example.com/docs)',
      ].join('\n'),
    )

    expect(output.querySelector('strong')?.textContent).toBe('重构建议')
    expect(output.querySelector('table')).not.toBeNull()
    expect(output.querySelector('a')?.getAttribute('href')).toBe('https://example.com/docs')
    expect(output.querySelector('a')?.getAttribute('rel')).toBe('noopener noreferrer')
  })

  it('preserves fenced code as inert code content', () => {
    const output = render(['```html', '<script>alert("code sample")</script>', '```'].join('\n'))
    const code = output.querySelector('pre code')

    expect(code?.textContent).toContain('<script>alert("code sample")</script>')
    expect(code?.querySelector('script')).toBeNull()
  })

  it('removes script and executable embedded elements', () => {
    const output = render(
      '<script>alert(1)</script><iframe src="https://example.com"></iframe><object></object><embed>',
    )

    expect(output.querySelector('script, iframe, object, embed')).toBeNull()
  })

  it('removes inline event handlers from images', () => {
    const output = render('<img src="https://example.com/avatar.png" onerror="alert(1)">')
    const image = output.querySelector('img')

    expect(image).not.toBeNull()
    expect(image?.hasAttribute('onerror')).toBe(false)
  })

  it.each([
    '[危险链接](javascript:alert(1))',
    '[危险链接](data:text/html,<script>alert(1)</script>)',
  ])('makes dangerous markdown links non-executable: %s', (markdown) => {
    const output = render(markdown)
    const link = output.querySelector('a')

    expect(link?.hasAttribute('href')).toBe(false)
  })
})
