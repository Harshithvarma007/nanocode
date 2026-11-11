import { memo, useMemo, useState } from 'react'
import { highlightLine, langForPath } from '../lib/highlight'

type Row =
  | { type: 'hunk'; text: string }
  | { type: 'add' | 'del' | 'ctx'; oldNo: number | null; newNo: number | null; text: string }

export function parseDiff(diff: string): { rows: Row[]; file: string; adds: number; dels: number } {
  const rows: Row[] = []
  let file = ''
  let oldNo = 0
  let newNo = 0
  let adds = 0
  let dels = 0
  for (const line of diff.split('\n')) {
    if (line.startsWith('+++ ')) {
      file = line.slice(4).replace(/^b\//, '').trim()
      continue
    }
    if (line.startsWith('--- ')) {
      if (!file) file = line.slice(4).replace(/^a\//, '').trim()
      continue
    }
    if (line.startsWith('@@')) {
      const m = /@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@(.*)/.exec(line)
      if (m) {
        oldNo = Number(m[1])
        newNo = Number(m[2])
      }
      rows.push({ type: 'hunk', text: m?.[3]?.trim() || line })
      continue
    }
    if (line.startsWith('\\')) continue
    if (line.startsWith('+')) {
      rows.push({ type: 'add', oldNo: null, newNo: newNo++, text: line.slice(1) })
      adds++
    } else if (line.startsWith('-')) {
      rows.push({ type: 'del', oldNo: oldNo++, newNo: null, text: line.slice(1) })
      dels++
    } else if (line.startsWith(' ') || (line === '' && rows.length)) {
      rows.push({ type: 'ctx', oldNo: oldNo++, newNo: newNo++, text: line.slice(1) })
    }
  }
  while (rows.length && rows[rows.length - 1].type === 'ctx' && (rows[rows.length - 1] as { text: string }).text === '') rows.pop()
  return { rows, file, adds, dels }
}

export function DiffStats({ adds, dels }: { adds: number; dels: number }) {
  return (
    <span className="diff-stats">
      <span className="a">+{adds}</span>
      <span className="d">−{dels}</span>
    </span>
  )
}

/** A unified-diff viewer: old/new line numbers, +/- colors, syntax highlighting, collapsible. */
export const DiffView = memo(function DiffView({ diff, path, maxRows = 40 }: { diff: string; path?: string; maxRows?: number }) {
  const parsed = useMemo(() => parseDiff(diff), [diff])
  const [expanded, setExpanded] = useState(false)
  const lang = langForPath(path || parsed.file)
  const visible = expanded ? parsed.rows : parsed.rows.slice(0, maxRows)
  const hidden = parsed.rows.length - visible.length
  const firstHunkOnly = parsed.rows.filter((r) => r.type === 'hunk').length <= 1
  return (
    <div className="diff">
      <table className="diff-table selectable">
        <tbody>
          {visible.map((row, i) => {
            if (row.type === 'hunk') {
              if (i === 0 && firstHunkOnly) return null
              return (
                <tr key={i} className="hunk">
                  <td colSpan={4}>⋯ {row.text.startsWith('@@') ? '' : row.text}</td>
                </tr>
              )
            }
            const html = highlightLine(row.text, lang)
            return (
              <tr key={i} className={row.type}>
                <td className="ln">{row.oldNo ?? ''}</td>
                <td className="ln">{row.newNo ?? ''}</td>
                <td className="sign">{row.type === 'add' ? '+' : row.type === 'del' ? '−' : ' '}</td>
                {html ? <td className="code" dangerouslySetInnerHTML={{ __html: html || ' ' }} /> : <td className="code">{row.text || ' '}</td>}
              </tr>
            )
          })}
        </tbody>
      </table>
      {(hidden > 0 || expanded) && parsed.rows.length > maxRows && (
        <div className="expand-row">
          <span>{expanded ? `${parsed.rows.length} lines` : `${hidden} more line${hidden === 1 ? '' : 's'}`}</span>
          <button className="link-btn" onClick={() => setExpanded(!expanded)}>
            {expanded ? 'Show less' : 'Show all'}
          </button>
        </div>
      )}
    </div>
  )
})
