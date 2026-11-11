import type { Mode } from '@shared/protocol'

export function fmtTokens(n: number): string {
  if (!Number.isFinite(n)) return '0'
  return n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(Math.round(n))
}

export function relativeTime(unixSeconds: number): string {
  const diff = Date.now() / 1000 - unixSeconds
  if (diff < 45) return 'just now'
  if (diff < 3600) return `${Math.round(diff / 60)}m ago`
  if (diff < 86400) return `${Math.round(diff / 3600)}h ago`
  if (diff < 86400 * 7) return `${Math.round(diff / 86400)}d ago`
  return new Date(unixSeconds * 1000).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
}

export function elapsed(ms: number): string {
  const s = Math.max(0, Math.floor(ms / 1000))
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  return `${m}m ${String(s % 60).padStart(2, '0')}s`
}

export function basename(p: string): string {
  return p.split('/').filter(Boolean).pop() ?? p
}

/** "/Users/me/code/shop" -> "~/code/shop" */
export function tildify(p: string, home: string): string {
  return home && p.startsWith(home) ? '~' + p.slice(home.length) : p
}

export const MODES: Record<Mode, { label: string; short: string; hint: string }> = {
  default: { label: 'Ask before edits', short: 'Default', hint: 'nanocode asks before every edit and command' },
  'accept-edits': { label: 'Accept edits', short: 'Accept edits', hint: 'File edits are applied without asking; commands still ask' },
  plan: { label: 'Plan mode', short: 'Plan', hint: 'Read-only research, then a plan for you to approve' },
  yolo: { label: 'Yolo', short: 'Yolo', hint: 'Every action is auto-approved' }
}

/** Model spec "provider:model" -> a short display name. */
export function modelName(spec: string): string {
  if (!spec || spec.startsWith('none:')) return 'Choose a model'
  const [provider, ...rest] = spec.split(':')
  const model = rest.join(':')
  if (provider === 'anthropic') {
    const m = model.match(/^claude-(opus|sonnet|haiku|fable)-(\d+)(?:-(\d+))?/)
    if (m) return `Claude ${m[1][0].toUpperCase()}${m[1].slice(1)} ${m[2]}${m[3] ? '.' + m[3] : ''}`
  }
  return model || spec
}

export function plural(n: number, word: string): string {
  return `${n} ${word}${n === 1 ? '' : 's'}`
}
