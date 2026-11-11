import { memo, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { ArrowDown, ChevronRight, CircleAlert, Copy, FlaskConical, Info, Layers, RotateCcw, ScrollText, TriangleAlert, WandSparkles } from 'lucide-react'
import { sendPrompt } from '../lib/actions'
import { basename, elapsed, fmtTokens } from '../lib/format'
import { useStore, type AssistantItem, type Item } from '../lib/store'
import { PermissionCard, PlanCard } from './Ask'
import { Markdown } from './Markdown'
import { ToolCard } from './ToolCard'
import { Glyph, Mark } from './ui'

// ------------------------------------------------------------------ items

function renderUserText(text: string) {
  let prefix: string | null = null
  let body = text
  if (text.startsWith('!')) {
    prefix = 'shell'
    body = text.slice(1).trimStart()
  } else if (text.startsWith('#') && !text.startsWith('##')) {
    prefix = 'memory'
    body = text.slice(1).trimStart()
  }
  const parts = body.split(/((?<![\w@])@[\w./~-]+)/g)
  return (
    <>
      {prefix && <span className="prefix-tag">{prefix === 'shell' ? '$' : '#'}</span>}
      {parts.map((p, i) =>
        p.startsWith('@') ? (
          <span key={i} className="mention">
            {p}
          </span>
        ) : p.startsWith('/') && i === 0 ? (
          <span key={i} className="mention" style={{ background: 'transparent', padding: 0 }}>
            {p}
          </span>
        ) : (
          p
        )
      )}
    </>
  )
}

const UserMessage = memo(function UserMessage({ item }: { item: Extract<Item, { kind: 'user' }> }) {
  const checkpoint = useStore((s) => s.checkpoints.find((c) => c.turn_id === item.turnId))
  const busy = useStore((s) => s.status.busy)
  return (
    <div className="msg-user item">
      <div className="msg-user-glyph">❯</div>
      <div className="msg-user-bubble selectable">
        {renderUserText(item.text)}
        <div className="msg-user-actions">
          <button className="icon-btn sm" title="Copy" onClick={() => void navigator.clipboard.writeText(item.text)}>
            <Copy size={12} />
          </button>
          {checkpoint && !busy && (
            <button className="icon-btn sm" title="Rewind to before this message" onClick={() => useStore.setState((s) => void (s.rewindTarget = checkpoint))}>
              <RotateCcw size={12} />
            </button>
          )}
        </div>
      </div>
    </div>
  )
})

function Thinking({ item }: { item: AssistantItem }) {
  const [open, setOpen] = useState(false)
  const live = item.streaming && !item.text
  const seconds = item.thinkingSeconds || (item.thinkStart && item.thinkEnd ? Math.round((item.thinkEnd - item.thinkStart) / 1000) : 0)
  const label = live ? 'Thinking…' : seconds >= 1 ? `Thought for ${seconds}s` : 'Thought'
  return (
    <div className="thinking">
      <button className="thinking-head" onClick={() => setOpen(!open)}>
        <ChevronRight size={13} className={`chev ${open ? 'open' : ''}`} />
        {label}
      </button>
      {open && <div className="thinking-body selectable">{item.thinking.trim()}</div>}
    </div>
  )
}

const AssistantMessage = memo(function AssistantMessage({ item }: { item: AssistantItem }) {
  const showThinking = useStore((s) => s.think)
  if (!item.text.trim() && !item.thinking.trim()) return null
  return (
    <div className="msg-assistant item">
      {item.thinking.trim() && (showThinking || item.streaming) && <Thinking item={item} />}
      {item.text.trim() && <Markdown text={item.text} streaming={item.streaming} />}
    </div>
  )
})

function Notice({ item }: { item: Extract<Item, { kind: 'notice' }> }) {
  const Icon = item.level === 'error' ? CircleAlert : item.level === 'warn' ? TriangleAlert : Info
  return (
    <div className={`notice ${item.level} item selectable`}>
      <Icon size={14} />
      <span>{item.message}</span>
    </div>
  )
}

function Compacted({ item }: { item: Extract<Item, { kind: 'compacted' }> }) {
  const [open, setOpen] = useState(false)
  return (
    <div className="compacted item">
      <div className="compacted-line">
        <button className="compacted-pill" onClick={() => setOpen(!open)}>
          <Layers size={13} style={{ color: 'var(--accent)' }} />
          <b>Conversation compacted</b>
          <span>
            {fmtTokens(item.before)} → {fmtTokens(item.after)} of {fmtTokens(item.limit)} tokens
          </span>
          <ChevronRight size={13} className={`chev ${open ? 'open' : ''}`} />
        </button>
      </div>
      {open && (
        <div className="compacted-summary">
          <Markdown text={item.summary} />
        </div>
      )}
    </div>
  )
}

function TranscriptItem({ item, latestTodo }: { item: Item; latestTodo: string }) {
  switch (item.kind) {
    case 'user':
      return <UserMessage item={item} />
    case 'assistant':
      return <AssistantMessage item={item} />
    case 'tool':
      return <ToolCard item={item} collapsed={item.name === 'todo_write' && item.id !== latestTodo} />
    case 'todos':
      return null
    case 'notice':
      return <Notice item={item} />
    case 'compacted':
      return <Compacted item={item} />
    case 'interrupted':
      return (
        <div className="interrupted item">
          <CircleAlert size={14} />
          Interrupted · What should nanocode do instead?
        </div>
      )
  }
}

// ------------------------------------------------------------------ status line

const LABELS: Record<string, string> = {
  read_file: 'Read', write_file: 'Write', edit_file: 'Update', bash: 'Bash', glob: 'Glob', grep: 'Grep',
  todo_write: 'Update Todos', exit_plan_mode: 'Plan', task: 'Task', web_fetch: 'Fetch'
}

export function StatusLine() {
  const turn = useStore((s) => s.turn)
  const busy = useStore((s) => s.status.busy)
  const preparing = useStore((s) => s.preparing[''])
  const waiting = useStore((s) => s.permissions.length > 0 || s.plan !== null)
  const rootThinking = useStore((s) => {
    const id = s.streams['']
    const it = id ? s.items.find((x) => x.id === id) : undefined
    return Boolean(it && it.kind === 'assistant' && it.thinking && !it.text)
  })
  const runningTool = useStore((s) => {
    for (let i = s.items.length - 1; i >= 0; i--) {
      const it = s.items[i]
      if (it.kind === 'tool') return !it.result && !it.stopped ? it.name : null
      if (it.kind === 'user') return null
    }
    return null
  })
  const [now, setNow] = useState(Date.now())
  const startRef = useRef(Date.now())
  useEffect(() => {
    if (!busy) return
    if (!turn) startRef.current = Date.now()
    const t = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(t)
  }, [busy, turn])
  if (!busy) return null
  const started = turn?.startedAt ?? startRef.current
  let label = (turn?.verb ?? 'Working') + '…'
  if (waiting) label = 'Waiting for your approval'
  else if (preparing) label = `Preparing ${preparing.startsWith('mcp__') ? 'MCP call' : LABELS[preparing] ?? preparing}…`
  else if (runningTool === 'bash') label = 'Running…'
  else if (runningTool === 'task') label = 'Subagent working…'
  else if (runningTool === 'grep' || runningTool === 'glob') label = 'Searching…'
  else if (rootThinking) label = 'Thinking…'
  else if (!turn) label = 'Compacting conversation…'
  const tokens = Math.round((turn?.chars ?? 0) / 4)
  return (
    <div className="statusline" aria-live="polite">
      <Glyph active={!waiting} />
      <span className="statusline-verb">{label}</span>
      <span className="statusline-meta">
        ({elapsed(now - started)}
        {tokens > 0 && ` · ↓ ${fmtTokens(tokens)} tokens`}
        {!waiting && ' · esc to interrupt'})
      </span>
    </div>
  )
}

// ------------------------------------------------------------------ empty state

function EmptyState() {
  const project = useStore((s) => s.project)
  const name = project ? basename(project) : 'this project'
  const chips = [
    { icon: <ScrollText size={14} />, text: 'Explain this project' },
    { icon: <FlaskConical size={14} />, text: 'Find and fix failing tests' },
    { icon: <WandSparkles size={14} />, text: '/init', label: '/init · write AGENTS.md' }
  ]
  return (
    <div className="empty">
      <div className="empty-mark">
        <Mark size={40} strokeWidth={2.2} />
      </div>
      <h1>What should we work on in {name}?</h1>
      <p>nanocode reads your code, edits files and runs commands. It asks before changing anything.</p>
      <div className="chips">
        {chips.map((c) => (
          <button key={c.text} className="chip" onClick={() => void sendPrompt(c.text)}>
            {c.icon}
            {c.label ?? c.text}
          </button>
        ))}
      </div>
      <div className="tips">
        <span>
          <kbd>@</kbd> attach a file
        </span>
        <span>
          <kbd>/</kbd> commands
        </span>
        <span>
          <kbd>!</kbd> run a shell command
        </span>
        <span>
          <kbd>#</kbd> save to memory
        </span>
        <span>
          <kbd>⇧</kbd>
          <kbd>⇥</kbd> switch modes
        </span>
      </div>
    </div>
  )
}

// ------------------------------------------------------------------ the transcript

export function Transcript({ onScrolled }: { onScrolled: (scrolled: boolean) => void }) {
  const items = useStore((s) => s.items)
  const permissions = useStore((s) => s.permissions)
  const plan = useStore((s) => s.plan)
  const sessionId = useStore((s) => s.session?.id)
  const scrollRef = useRef<HTMLDivElement>(null)
  const columnRef = useRef<HTMLDivElement>(null)
  const stick = useRef(true)
  const [showJump, setShowJump] = useState(false)

  // Keep the view pinned to the bottom while content streams in, unless the user scrolled up.
  useLayoutEffect(() => {
    const el = scrollRef.current
    const col = columnRef.current
    if (!el || !col) return
    const ro = new ResizeObserver(() => {
      if (stick.current) el.scrollTop = el.scrollHeight
    })
    ro.observe(col)
    return () => ro.disconnect()
  }, [])

  useLayoutEffect(() => {
    stick.current = true
    const el = scrollRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [sessionId])

  // Only a scroll *up* (by the user) unpins the view; content growing or smooth scrolls never do.
  const lastTop = useRef(0)
  const onScroll = () => {
    const el = scrollRef.current
    if (!el) return
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 60
    if (atBottom) stick.current = true
    else if (el.scrollTop < lastTop.current - 1) stick.current = false
    lastTop.current = el.scrollTop
    setShowJump(!stick.current)
    onScrolled(el.scrollTop > 4)
  }

  let latestTodo = ''
  for (let i = items.length - 1; i >= 0 && !latestTodo; i--) {
    const it = items[i]
    if (it.kind === 'tool' && it.name === 'todo_write') latestTodo = it.id
  }
  const empty = items.length === 0 && !permissions.length && !plan
  return (
    <div style={{ position: 'relative', flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' }}>
    <div className="scroll" ref={scrollRef} onScroll={onScroll}>
      <div ref={columnRef} className={empty ? '' : 'column'} style={empty ? { height: '100%' } : undefined}>
        {empty ? (
          <EmptyState />
        ) : (
          <>
            {items.map((item) => (
              <TranscriptItem key={item.id} item={item} latestTodo={latestTodo} />
            ))}
            {permissions.map((p, i) => (
              <PermissionCard key={p.requestId} req={p} active={i === 0} />
            ))}
            {plan && <PlanCard req={plan} />}
            <StatusLine />
          </>
        )}
      </div>
    </div>
      {showJump && !empty && (
        <button
          className="btn jump"
          onClick={() => {
            const el = scrollRef.current
            stick.current = true
            setShowJump(false)
            if (el) el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' })
          }}
        >
          <ArrowDown size={14} /> Jump to latest
        </button>
      )}
    </div>
  )
}
