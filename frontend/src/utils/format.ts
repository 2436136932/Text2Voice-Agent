/**
 * Small, dependency-free formatting helpers shared by the views.
 */

/** Format an ISO timestamp as `YYYY-MM-DD HH:mm:ss` (local time). */
export function formatDateTime(value: string | null | undefined): string {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return String(value)
  const pad = (input: number): string => String(input).padStart(2, '0')
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
    `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
  )
}

/** Format a millisecond duration as seconds with two decimals, e.g. `4.21s`. */
export function formatSeconds(ms: number | null | undefined, suffix = 's'): string {
  if (ms === null || ms === undefined || !Number.isFinite(ms)) return '—'
  return `${(ms / 1000).toFixed(2)}${suffix}`
}

/** Format a byte count as `B` / `KB` / `MB`. */
export function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined || !Number.isFinite(bytes)) return '—'
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(2)} MB`
}

/** Truncate text for table cells. */
export function truncate(value: string | null | undefined, max = 60): string {
  if (!value) return ''
  const normalized = value.replace(/\s+/g, ' ').trim()
  return normalized.length > max ? `${normalized.slice(0, max)}…` : normalized
}

/** Human label for a task status. */
export function statusLabel(status: string): string {
  switch (status) {
    case 'pending':
      return '等待中'
    case 'running':
      return '进行中'
    case 'succeeded':
      return '成功'
    case 'failed':
      return '失败'
    case 'canceled':
      return '已取消'
    default:
      return status
  }
}

/** Element Plus tag type for a task status. */
export function statusTagType(status: string): 'success' | 'warning' | 'danger' | 'info' {
  switch (status) {
    case 'succeeded':
      return 'success'
    case 'running':
      return 'warning'
    case 'failed':
      return 'danger'
    default:
      return 'info'
  }
}

/** Chinese label for a task stage. */
export function stageLabel(stage: string | null | undefined): string {
  switch (stage) {
    case 'queued':
      return '排队中'
    case 'llm':
      return '正在调用大模型…'
    case 'tts':
      return '正在合成语音…'
    case 'persist':
      return '正在保存结果…'
    case 'done':
      return '完成'
    default:
      return stage ? String(stage) : '准备中'
  }
}

/** Human label for a voice gender. */
export function genderLabel(gender: string | null | undefined): string {
  switch (gender) {
    case 'male':
      return '男声'
    case 'female':
      return '女声'
    case 'neutral':
      return '中性'
    default:
      return gender ? String(gender) : '未标注'
  }
}

/** Copy text to the clipboard, falling back to a hidden textarea. */
export async function copyText(value: string): Promise<boolean> {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(value)
      return true
    }
  } catch {
    /* fall through to the legacy path */
  }
  try {
    const area = document.createElement('textarea')
    area.value = value
    area.setAttribute('readonly', 'readonly')
    area.style.position = 'fixed'
    area.style.top = '-1000px'
    document.body.appendChild(area)
    area.select()
    const ok = document.execCommand('copy')
    document.body.removeChild(area)
    return ok
  } catch {
    return false
  }
}