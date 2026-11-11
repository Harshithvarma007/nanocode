import { useState } from 'react'
import { FileCode, Gauge, GitCompare, ListTodo, Plug, Receipt, RefreshCw, RotateCcw, TriangleAlert, Undo2 } from 'lucide-react'
import type { Checkpoint } from '@shared/protocol'
import { compact, refreshInspector, restoreCheckpoint, setInspectorTab, toggleInspector } from '../lib/actions'
import { fmtTokens, plural, relativeTime } from '../lib/format'
import { setState, useStore, type InspectorTab } from '../lib/store'
import { TodoList } from './ToolCard'
import { Dialog } from './ui'

const TABS: { id: InspectorTab; label: string; icon: React.ReactNode }[] = [
  { id: 'todos', label: 'Todos', icon: <ListTodo size={13} /> },
  { id: 'changes', label: 'Changes', icon: <GitCompare size={13} /> },
  { id: 'context', label: 'Context', icon: <Gauge size={13} /> },
  { id: 'mcp', label: 'MCP', icon: <Plug size={13} /> },
  { id: 'usage', label: 'Usage', icon: <Receipt size={13} /> }
]

function Empty({ icon, children }: { icon: React.ReactNode; children: React.ReactNode }) {
  return (
    <div className="insp-empty">
      {icon}
      <div>{children}</div>
    </div>
  )
}

function TodosTab() {
  const todos = useStore((s) => s.todos)
  if (!todos.length)
    return (
      <Empty icon={<ListTodo size={28} />}>
        No todos yet. nanocode writes a todo list when a task has several steps.
      </Empty>
    )
  const done = todos.filter((t) => t.status === 'completed').length
  return (
    <>
      <div className="insp-section-title">
        <span>Progress</span>
        <span style={{ color: 'var(--text-3)', fontWeight: 500 }}>
          {done} of {todos.length} done
        </span>
      </div>
      <div className="progress">
        <div style={{ width: `${(100 * done) / todos.length}%` }} />
      </div>
      <div className="card insp-todos">
        <TodoList todos={todos} />
      </div>
    </>
  )
}

function ChangesTab() {
  const checkpoints = useStore((s) => s.checkpoints)
  const busy = useStore((s) => s.status.busy)
  if (!checkpoints.length)
    return (
      <Empty icon={<GitCompare size={28} />}>
        No changes in this session yet. Every turn that edits files gets a checkpoint you can rewind to.
      </Empty>
    )
  return (
    <>
      <div className="insp-section-title">
        <span>Checkpoints</span>
        <span style={{ color: 'var(--text-3)', fontWeight: 500 }}>{plural(checkpoints.length, 'turn')}</span>
      </div>
      {[...checkpoints].reverse().map((cp, i) => (
        <div key={cp.turn_id} className="card cp">
          <div className="cp-top">
            <div className="cp-prompt" title={cp.prompt}>
              {cp.prompt || 'Turn ' + cp.turn_id}
            </div>
            <span className="cp-turn">#{checkpoints.length - i}</span>
          </div>
          <ul className="cp-files">
            {cp.files.map((f) => (
              <li key={f} title={f}>
                <FileCode size={12} />
                {f}
              </li>
            ))}
          </ul>
          <div className="cp-foot">
            <span>
              {relativeTime(cp.created)} · {plural(cp.files.length, 'file')}
            </span>
            <button className="btn sm" disabled={busy} onClick={() => setState((s) => void (s.rewindTarget = cp))}>
              <RotateCcw size={12} />
              Rewind
            </button>
          </div>
        </div>
      ))}
      <p className="note">Rewinding puts files back to how they were before that turn (and undoes every later turn). Shell-command side effects are not undone.</p>
    </>
  )
}

function ContextTab() {
  const ctx = useStore((s) => s.context)
  const busy = useStore((s) => s.status.busy)
  if (!ctx) return <Empty icon={<Gauge size={28} />}>Loading context usage…</Empty>
  const limit = ctx.limit || 1
  const pct = (n: number) => `${Math.min(100, (100 * n) / limit)}%`
  const free = Math.max(0, ctx.limit - ctx.used)
  const segments = [
    { label: 'System prompt', n: ctx.system, color: '#7aa2f7' },
    { label: 'Tools', n: ctx.tools, color: '#bb9af7' },
    { label: 'Messages', n: ctx.messages, color: 'var(--accent)' }
  ]
  return (
    <>
      <div className="ctx-big">
        <b>{Math.round((100 * ctx.used) / limit)}%</b>
        <span>
          {fmtTokens(ctx.used)} / {fmtTokens(ctx.limit)} tokens
        </span>
      </div>
      <div className="ctx-track">
        <div className="ctx-bar">
          {segments.map((s) => (
            <div key={s.label} style={{ width: pct(s.n), background: s.color }} title={`${s.label}: ${fmtTokens(s.n)}`} />
          ))}
        </div>
        <span className="ctx-mark" style={{ left: `${ctx.microcompact_at * 100}%` }}>
          <span>{Math.round(ctx.microcompact_at * 100)}% trim</span>
        </span>
        <span className="ctx-mark" style={{ left: `${ctx.compact_at * 100}%` }}>
          <span>{Math.round(ctx.compact_at * 100)}% compact</span>
        </span>
      </div>
      <div className="legend">
        {segments.map((s) => (
          <Legend key={s.label} color={s.color} label={s.label} value={s.n} />
        ))}
        <Legend color="var(--surface-3)" label="Free" value={free} />
      </div>
      <p className="note">
        Old tool results are cleared at {Math.round(ctx.microcompact_at * 100)}%, and the conversation is summarized automatically at{' '}
        {Math.round(ctx.compact_at * 100)}%.
        {ctx.window > ctx.limit && ` The budget is capped at ${fmtTokens(ctx.limit)} of the model's ${fmtTokens(ctx.window)} window.`}
      </p>
      <button className="btn" style={{ marginTop: 12, width: '100%' }} disabled={busy || ctx.messages < 200} onClick={() => void compact()}>
        Compact now
      </button>
    </>
  )
}

function Legend({ color, label, value }: { color: string; label: string; value: number }) {
  return (
    <>
      <span className="sw" style={{ background: color }} />
      <span>{label}</span>
      <span className="val">{fmtTokens(value)}</span>
    </>
  )
}

function McpTab() {
  const servers = useStore((s) => s.mcp)
  if (!servers.length)
    return (
      <Empty icon={<Plug size={28} />}>
        No MCP servers. Add them to <code>.mcp.json</code> in the project (or <code>~/.nanocode/mcp.json</code>) and restart the engine.
      </Empty>
    )
  return (
    <>
      <div className="insp-section-title">
        <span>Servers</span>
        <span style={{ color: 'var(--text-3)', fontWeight: 500 }}>{servers.filter((s) => s.status === 'connected').length} connected</span>
      </div>
      {servers.map((s) => (
        <div key={s.name} className="card mcp-server">
          <div className="mcp-top">
            <span className={`dot ${s.status === 'connected' ? 'ok' : s.status === 'failed' ? 'err' : 'off'}`} />
            {s.name}
            <span className="mcp-status">{s.status === 'connected' ? plural(s.tools.length, 'tool') : s.status}</span>
          </div>
          {s.error && <div className="mcp-error selectable">{s.error}</div>}
          {s.tools.length > 0 && (
            <div className="mcp-tools">
              {s.tools.map((t) => (
                <span key={t}>{t}</span>
              ))}
            </div>
          )}
        </div>
      ))}
    </>
  )
}

function UsageTab() {
  const rows = useStore((s) => s.cost)
  if (!rows.length) return <Empty icon={<Receipt size={28} />}>No model calls yet in this session.</Empty>
  const dollars = rows.reduce((sum, r) => {
    const m = /^\$([\d.]+)/.exec(r.cost)
    return sum + (m ? Number(m[1]) : 0)
  }, 0)
  const tracked = rows.some((r) => r.cost.startsWith('$'))
  const allLocal = rows.every((r) => r.cost.includes('local'))
  return (
    <>
      <div className="usage-total">
        <span style={{ color: 'var(--text-3)', fontSize: 12.5 }}>{allLocal ? 'Local models cost nothing' : 'Estimated cost'}</span>
        <b>{allLocal ? 'Free' : tracked ? `$${dollars.toFixed(dollars < 1 ? 4 : 2)}` : '—'}</b>
      </div>
      <div className="card" style={{ padding: '2px 6px' }}>
        <table className="usage-table">
          <thead>
            <tr>
              <th>Model</th>
              <th>Input</th>
              <th>Cached</th>
              <th>Output</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.model}>
                <td>
                  {r.model}
                  <div style={{ fontFamily: 'var(--font-ui)', color: 'var(--text-3)', fontSize: 11.5, marginTop: 2 }}>{r.cost}</div>
                </td>
                <td>{fmtTokens(r.input)}</td>
                <td>{fmtTokens(r.cached)}</td>
                <td>{fmtTokens(r.output)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="note">Token counts are for this session. Local models are free; prices for Claude come from the engine's price table.</p>
    </>
  )
}

export function Inspector() {
  const open = useStore((s) => s.inspectorOpen)
  const tab = useStore((s) => s.inspectorTab)
  const todoCount = useStore((s) => s.todos.filter((t) => t.status !== 'completed').length)
  const changeCount = useStore((s) => s.checkpoints.length)
  return (
    <aside className={`inspector ${open ? '' : 'closed'}`} aria-hidden={!open}>
      <div className="insp-head drag">
        <div className="insp-tabs">
          {TABS.map((t) => (
            <button key={t.id} className={`insp-tab ${tab === t.id ? 'active' : ''}`} onClick={() => setInspectorTab(t.id)} title={t.label}>
              {t.icon}
              {tab === t.id && t.label}
              {t.id === 'todos' && todoCount > 0 && <span className="count">{todoCount}</span>}
              {t.id === 'changes' && changeCount > 0 && <span className="count">{changeCount}</span>}
            </button>
          ))}
        </div>
        <div style={{ flex: 1 }} />
        <button className="icon-btn" title="Refresh" onClick={() => void refreshInspector()}>
          <RefreshCw size={14} />
        </button>
        <button className="icon-btn" title="Hide inspector (⌥⌘I)" onClick={toggleInspector}>
          <PanelRightIcon />
        </button>
      </div>
      <div className="insp-body">
        {tab === 'todos' && <TodosTab />}
        {tab === 'changes' && <ChangesTab />}
        {tab === 'context' && <ContextTab />}
        {tab === 'mcp' && <McpTab />}
        {tab === 'usage' && <UsageTab />}
      </div>
    </aside>
  )
}

function PanelRightIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round">
      <rect x="3" y="3" width="18" height="18" rx="2" />
      <path d="M15 3v18" />
    </svg>
  )
}

// ------------------------------------------------------------------ rewind

export function RewindDialog({ cp }: { cp: Checkpoint }) {
  const [conversation, setConversation] = useState<boolean | null>(null)
  const [working, setWorking] = useState(false)
  const close = () => setState((s) => void (s.rewindTarget = null))
  const all = useStore((s) => s.checkpoints)
  const index = all.findIndex((c) => c.turn_id === cp.turn_id)
  const later = index >= 0 ? all.slice(index + 1) : []
  const files = [...new Set([cp, ...later].flatMap((c) => c.files))]

  if (conversation === null) {
    return (
      <Dialog title="Rewind" icon={<RotateCcw size={18} style={{ color: 'var(--accent)' }} />} onClose={close} footer={<button className="btn" onClick={close}>Cancel</button>}>
        <div style={{ marginBottom: 12 }}>
          Go back to before <b style={{ color: 'var(--text)' }}>“{cp.prompt}”</b>
          {later.length > 0 && ` (and ${plural(later.length, 'later turn')})`}.
        </div>
        <button className="choice" onClick={() => setConversation(false)}>
          <Undo2 size={16} />
          <span>
            <div className="choice-title">Restore code</div>
            <div className="choice-desc">Put {plural(files.length, 'file')} back as they were. The conversation stays.</div>
          </span>
        </button>
        <button className="choice" onClick={() => setConversation(true)}>
          <RotateCcw size={16} />
          <span>
            <div className="choice-title">Restore code and conversation</div>
            <div className="choice-desc">Also rewind the chat to just before that message, so you can try again.</div>
          </span>
        </button>
      </Dialog>
    )
  }
  return (
    <Dialog
      title={conversation ? 'Restore code and conversation?' : 'Restore code?'}
      icon={<TriangleAlert size={18} style={{ color: 'var(--warn)' }} />}
      onClose={close}
      footer={
        <>
          <button className="btn" onClick={() => setConversation(null)}>
            Back
          </button>
          <button
            className="btn primary"
            disabled={working}
            autoFocus
            onClick={async () => {
              setWorking(true)
              await restoreCheckpoint(cp.turn_id, conversation)
              setWorking(false)
              close()
            }}
          >
            {working ? 'Restoring…' : 'Restore'}
          </button>
        </>
      }
    >
      <div>These files will be put back to how they were before this turn:</div>
      <ul className="cp-files" style={{ margin: '10px 0' }}>
        {files.map((f) => (
          <li key={f}>
            <FileCode size={12} />
            {f}
          </li>
        ))}
      </ul>
      <div style={{ fontSize: 12.5, color: 'var(--text-3)' }}>
        {conversation ? 'The conversation is rewound too, and your message goes back into the message box. ' : ''}
        Side effects of shell commands (installed packages, deleted files) are not undone.
      </div>
    </Dialog>
  )
}
