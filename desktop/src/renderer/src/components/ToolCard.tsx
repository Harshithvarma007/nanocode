import { memo, useState, type ReactNode } from 'react'
import {
  Bot,
  Check,
  ChevronRight,
  ClipboardList,
  FilePen,
  FilePlus,
  FileText,
  FolderSearch,
  Globe,
  ListTodo,
  Plug,
  Search,
  SquareTerminal,
  Wrench
} from 'lucide-react'
import type { Todo } from '@shared/protocol'
import type { Item, ToolItem } from '../lib/store'
import { plural } from '../lib/format'
import { DiffStats, DiffView, parseDiff } from './DiffView'
import { Markdown } from './Markdown'

export function toolIcon(name: string, size = 14): ReactNode {
  const props = { size, strokeWidth: 1.9 }
  if (name.startsWith('mcp__')) return <Plug {...props} />
  switch (name) {
    case 'read_file':
      return <FileText {...props} />
    case 'write_file':
      return <FilePlus {...props} />
    case 'edit_file':
      return <FilePen {...props} />
    case 'bash':
      return <SquareTerminal {...props} />
    case 'glob':
      return <FolderSearch {...props} />
    case 'grep':
      return <Search {...props} />
    case 'todo_write':
      return <ListTodo {...props} />
    case 'exit_plan_mode':
      return <ClipboardList {...props} />
    case 'task':
      return <Bot {...props} />
    case 'web_fetch':
    case 'web_search':
      return <Globe {...props} />
    default:
      return <Wrench {...props} />
  }
}

export function TodoList({ todos }: { todos: Todo[] }) {
  if (!todos.length) return <div className="palette-empty">The todo list is empty.</div>
  return (
    <ul className="todos">
      {todos.map((t, i) => (
        <li key={i} className={`todo ${t.status}`}>
          <span className="todo-box">{t.status === 'completed' && <Check size={10} strokeWidth={3.5} />}</span>
          <span className="todo-text">{t.content}</span>
        </li>
      ))}
    </ul>
  )
}

export function Output({ text, previewLines = 6 }: { text: string; previewLines?: number }) {
  const [all, setAll] = useState(false)
  const lines = text.replace(/\s+$/, '').split('\n')
  const shown = all ? lines : lines.slice(0, previewLines)
  const more = lines.length - shown.length
  return (
    <>
      <pre className="output selectable">{text.trim() ? shown.join('\n') : '(no output)'}</pre>
      {(more > 0 || all) && lines.length > previewLines && (
        <div className="expand-row">
          <span>{all ? plural(lines.length, 'line') : `+${plural(more, 'more line')}`}</span>
          <button className="link-btn" onClick={() => setAll(!all)}>
            {all ? 'Show less' : 'Show all'}
          </button>
        </div>
      )}
    </>
  )
}

function StatusIcon({ item }: { item: ToolItem }) {
  if (item.result) return <span className={`dot ${item.result.isError ? 'err' : 'ok'}`} />
  if (item.stopped) return <span className="dot off" />
  if (item.awaiting) return <span className="dot" style={{ background: 'var(--accent)', animation: 'pulse 1.2s ease-in-out infinite' }} />
  return <span className="spinner-ring" />
}

/** The subagent's last words, used as the Task card's report. */
function lastText(items: Item[]): string {
  for (let i = items.length - 1; i >= 0; i--) {
    const it = items[i]
    if (it.kind === 'assistant' && it.text.trim()) return it.text
  }
  return ''
}

function countTools(items: Item[]): number {
  return items.filter((i) => i.kind === 'tool').length
}

function defaultOpen(item: ToolItem, collapsed: boolean): boolean {
  if (collapsed) return false
  if (item.name === 'task') return !item.result
  if (item.result?.detailKind === 'diff') return true
  if (item.name === 'todo_write') return true
  if (item.result?.detailKind === 'text') return ['bash', 'web_fetch'].includes(item.name) || item.result.isError
  return false
}

export const ToolCard = memo(function ToolCard({ item, collapsed = false }: { item: ToolItem; collapsed?: boolean }) {
  const [openState, setOpen] = useState<boolean | null>(null)
  const open = openState ?? defaultOpen(item, collapsed)
  const r = item.result
  const isTask = item.name === 'task'
  const plan = item.name === 'exit_plan_mode' ? String(item.args.plan ?? '') : ''
  const todos = item.todos ?? (item.name === 'todo_write' && Array.isArray(item.args.todos) ? (item.args.todos as Todo[]) : undefined)
  const diff = r?.detailKind === 'diff' && r.detail ? r.detail : ''
  const stats = diff ? parseDiff(diff) : null
  const report = isTask ? lastText(item.children) || (r?.detail ?? '') : ''

  let body: ReactNode = null
  if (isTask) {
    body = (
      <>
        {(open || !r) && item.children.length > 0 && (
          <div className="subagent">
            <SubTranscript items={item.children} done={Boolean(r)} />
          </div>
        )}
        {r && report && (
          <div className="task-report">
            <div className="task-report-label">Report</div>
            <Markdown text={report} />
          </div>
        )}
      </>
    )
  } else if (r?.isError && !(r.detailKind === 'text' && r.detail)) {
    body = r.summary ? <div className="tool-error selectable">{r.summary}</div> : null
  } else if (diff) {
    body = <DiffView diff={diff} path={String(item.args.path ?? item.arg)} />
  } else if (todos && (r || item.name === 'todo_write')) {
    body = <TodoList todos={todos} />
  } else if (plan) {
    body = (
      <div style={{ padding: '10px 14px' }}>
        <Markdown text={plan} />
      </div>
    )
  } else if (r?.detail) {
    body = <Output text={r.detail} />
  }

  const expandable = Boolean(body) && !(isTask && !item.children.length && !report)
  const showBody = expandable && (open || (isTask && !r))
  const label = item.label || item.name
  const arg = item.name === 'todo_write' ? '' : item.arg

  let right: ReactNode = null
  if (item.awaiting && !r) right = <span className="tool-badge">Needs approval</span>
  else if (item.stopped && !r) right = <span className="tool-summary">Interrupted</span>
  else if (stats && !r?.isError) right = <DiffStats adds={stats.adds} dels={stats.dels} />
  else if (isTask && !r) right = <span className="tool-summary">{plural(countTools(item.children), 'tool use')}</span>
  else if (r && todos && item.name === 'todo_write') right = <span className="tool-summary">{r.summary}</span>
  else if (r?.summary && r.summary !== 'Done') right = <span className={`tool-summary ${r.isError ? 'err' : ''}`} title={r.summary}>{r.summary}</span>

  return (
    <div className="tool item">
      <button className={`tool-head ${expandable ? 'clickable' : ''}`} onClick={() => expandable && setOpen(!open)} title={item.arg}>
        <span className="tool-status">
          <StatusIcon item={item} />
        </span>
        <span className="tool-icon">{toolIcon(item.name)}</span>
        <span className="tool-label">{label}</span>
        <span className="tool-arg">{arg}</span>
        {right}
        {expandable && <ChevronRight size={14} className={`chev ${showBody ? 'open' : ''}`} style={{ color: 'var(--text-3)' }} />}
      </button>
      {showBody && <div className="tool-body">{body}</div>}
    </div>
  )
})

/** Items inside a Task card (the subagent's own text and tool calls). Once the task is done its
 *  final text is shown as the report, so it's left out here. */
function SubTranscript({ items, done }: { items: Item[]; done: boolean }) {
  let lastTextId = ''
  if (done) for (const it of items) if (it.kind === 'assistant' && it.text.trim()) lastTextId = it.id
  return (
    <>
      {items.map((it) => {
        if (it.kind === 'tool') return <ToolCard key={it.id} item={it} />
        if (it.kind === 'assistant') {
          if (!it.text.trim() || it.id === lastTextId) return null
          return (
            <div key={it.id} className="subagent-text item" style={{ margin: '6px 0' }}>
              <Markdown text={it.text} streaming={it.streaming} />
            </div>
          )
        }
        return null
      })}
    </>
  )
}
