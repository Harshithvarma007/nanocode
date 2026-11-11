// All renderer state, and the reducer that turns engine events into transcript items. The same
// reducer renders live events and `session.load` history, so both look identical.
import { create } from 'zustand'
import { immer } from 'zustand/middleware/immer'
import type { EngineState, SettingsState, WindowContext } from '@shared/host'
import type {
  Checkpoint,
  CommandInfo,
  ContextInfo,
  CostRow,
  DetailKind,
  EngineEvent,
  InitializeResult,
  McpServer,
  PreviewKind,
  SessionInfo,
  Status,
  Todo
} from '@shared/protocol'

export const VERBS = ['Thinking', 'Pondering', 'Noodling', 'Percolating', 'Cogitating', 'Tinkering', 'Scheming', 'Brewing',
  'Mulling', 'Puzzling', 'Conjuring', 'Crafting', 'Wrangling', 'Untangling', 'Simmering', 'Hatching']

export interface ToolResult {
  summary: string
  detail: string
  detailKind: DetailKind
  isError: boolean
}

export type Item =
  | { kind: 'user'; id: string; turnId?: string; text: string }
  | {
      kind: 'assistant'
      id: string
      text: string
      thinking: string
      thinkingSeconds: number
      streaming: boolean
      thinkStart?: number
      thinkEnd?: number
    }
  | {
      kind: 'tool'
      id: string
      callId: string
      name: string
      label: string
      arg: string
      args: Record<string, unknown>
      result?: ToolResult
      todos?: Todo[]
      children: Item[]
      startedAt: number
      endedAt?: number
      stopped?: boolean
      awaiting?: boolean
    }
  | { kind: 'todos'; id: string; todos: Todo[] }
  | { kind: 'notice'; id: string; level: 'info' | 'warn' | 'error'; message: string }
  | { kind: 'compacted'; id: string; before: number; after: number; limit: number; summary: string }
  | { kind: 'interrupted'; id: string }

export type ToolItem = Extract<Item, { kind: 'tool' }>
export type AssistantItem = Extract<Item, { kind: 'assistant' }>

export interface PermissionRequest {
  requestId: string
  callId: string
  name: string
  title: string
  question: string
  preview: string
  previewKind: PreviewKind
  alwaysLabel: string
}

export interface PlanRequest {
  requestId: string
  plan: string
}

export type Phase = 'boot' | 'welcome' | 'starting' | 'ready' | 'not-found' | 'crashed'
export type InspectorTab = 'todos' | 'changes' | 'context' | 'mcp' | 'usage'

export interface Turn {
  id: string
  prompt: string
  startedAt: number
  verb: string
  chars: number
}

export interface Toast {
  id: number
  message: string
  tone: 'info' | 'error'
}

const EMPTY_STATUS: Status = { mode: 'default', model: '', busy: false, context_used: 0, context_limit: 0 }

export interface State {
  phase: Phase
  ctx: WindowContext | null
  project: string | null
  engineState: EngineState
  restarting: string | null
  version: string
  commands: CommandInfo[]
  keys: Record<string, boolean>
  status: Status
  effort: string
  think: boolean
  session: SessionInfo | null
  sessions: SessionInfo[]
  items: Item[]
  streams: Record<string, string>
  preparing: Record<string, string | null>
  permissions: PermissionRequest[]
  plan: PlanRequest | null
  todos: Todo[]
  checkpoints: Checkpoint[]
  turn: Turn | null
  context: ContextInfo | null
  cost: CostRow[]
  mcp: McpServer[]
  settings: SettingsState | null
  draft: string
  inspectorOpen: boolean
  inspectorTab: InspectorTab
  sidebarOpen: boolean
  settingsOpen: boolean
  shortcutsOpen: boolean
  modelPickerOpen: boolean
  rewindTarget: Checkpoint | null
  toasts: Toast[]
}

let uidCounter = 0
const uid = () => `i${++uidCounter}`

function loadUi<T>(key: string, fallback: T): T {
  try {
    const raw = localStorage.getItem(`nano.ui.${key}`)
    return raw === null ? fallback : (JSON.parse(raw) as T)
  } catch {
    return fallback
  }
}

export function saveUi(key: string, value: unknown): void {
  try {
    localStorage.setItem(`nano.ui.${key}`, JSON.stringify(value))
  } catch {
    // per-viewer convenience only
  }
}

const initial: State = {
  phase: 'boot',
  ctx: null,
  project: null,
  engineState: { state: 'idle' },
  restarting: null,
  version: '',
  commands: [],
  keys: {},
  status: EMPTY_STATUS,
  effort: 'high',
  think: true,
  session: null,
  sessions: [],
  items: [],
  streams: {},
  preparing: {},
  permissions: [],
  plan: null,
  todos: [],
  checkpoints: [],
  turn: null,
  context: null,
  cost: [],
  mcp: [],
  settings: null,
  draft: '',
  inspectorOpen: loadUi('inspectorOpen', true),
  inspectorTab: loadUi<InspectorTab>('inspectorTab', 'todos'),
  sidebarOpen: loadUi('sidebarOpen', true),
  settingsOpen: false,
  shortcutsOpen: false,
  modelPickerOpen: false,
  rewindTarget: null,
  toasts: []
}

export const useStore = create<State>()(immer(() => initial))
export const getState = useStore.getState
export const setState = useStore.setState

// ------------------------------------------------------------------ transcript helpers

export function findTool(items: Item[], callId: string): ToolItem | undefined {
  for (let i = items.length - 1; i >= 0; i--) {
    const item = items[i]
    if (item.kind !== 'tool') continue
    if (item.callId === callId) return item
    const nested = findTool(item.children, callId)
    if (nested) return nested
  }
  return undefined
}

function container(s: State, parent?: string): Item[] {
  if (!parent) return s.items
  return findTool(s.items, parent)?.children ?? s.items
}

function streamItem(s: State, parent?: string): AssistantItem | undefined {
  const id = s.streams[parent ?? '']
  if (!id) return undefined
  const list = container(s, parent)
  for (let i = list.length - 1; i >= 0; i--) {
    const it = list[i]
    if (it.id === id && it.kind === 'assistant') return it
  }
  return undefined
}

function startStream(s: State, parent?: string): AssistantItem {
  const item: AssistantItem = { kind: 'assistant', id: uid(), text: '', thinking: '', thinkingSeconds: 0, streaming: true }
  container(s, parent).push(item)
  s.streams[parent ?? ''] = item.id
  s.preparing[parent ?? ''] = null
  return item
}

function removeItem(list: Item[], id: string): void {
  const i = list.findIndex((it) => it.id === id)
  if (i >= 0) list.splice(i, 1)
}

function finalizeStreams(s: State): void {
  for (const [key, id] of Object.entries(s.streams)) {
    const list = container(s, key || undefined)
    const it = list.find((x) => x.id === id)
    if (it && it.kind === 'assistant') {
      it.streaming = false
      if (!it.text.trim() && !it.thinking.trim()) removeItem(list, id)
    }
  }
  s.streams = {}
  s.preparing = {}
}

function markStopped(items: Item[]): void {
  for (const it of items) {
    if (it.kind !== 'tool') continue
    if (!it.result) {
      it.stopped = true
      it.awaiting = false
    }
    markStopped(it.children)
  }
}

function parseTodos(detail: string): Todo[] | undefined {
  try {
    const value = JSON.parse(detail)
    const list = Array.isArray(value) ? value : value?.todos
    if (Array.isArray(list) && list.every((t) => t && typeof t.content === 'string')) return list as Todo[]
  } catch {
    // not JSON: the `todos` event carries the list instead
  }
  return undefined
}

function lastTodoTool(items: Item[]): ToolItem | undefined {
  for (let i = items.length - 1; i >= 0; i--) {
    const it = items[i]
    if (it.kind === 'tool' && it.name === 'todo_write') return it
    if (it.kind === 'user') return undefined
  }
  return undefined
}

/** Apply one engine event. `live` is false when replaying a saved transcript. */
export function applyEvent(s: State, ev: EngineEvent, live: boolean): void {
  switch (ev.event) {
    case 'status': {
      const { event: _e, ...status } = ev
      s.status = status
      if (!status.busy && s.turn && live) s.turn = null
      break
    }
    case 'user':
      finalizeStreams(s)
      s.items.push({ kind: 'user', id: uid(), turnId: ev.turn_id, text: ev.text })
      if (live) {
        s.turn = { id: ev.turn_id, prompt: ev.text, startedAt: Date.now(), verb: VERBS[Math.floor(Math.random() * VERBS.length)], chars: 0 }
        // The engine names a session after its first prompt but only reports it when the turn ends;
        // show the same title right away.
        const open = s.session
        if (open && open.title === 'New session' && !/^[/!#]/.test(ev.text)) {
          open.title = ev.text.split(/\s+/).join(' ').slice(0, 60)
          const listed = s.sessions.find((x) => x.id === open.id)
          if (listed) listed.title = open.title
          else s.sessions.unshift({ ...open })
        }
      }
      break
    case 'stream.start': {
      const existing = streamItem(s, ev.parent)
      if (existing) {
        existing.streaming = false
        if (!existing.text.trim() && !existing.thinking.trim()) removeItem(container(s, ev.parent), existing.id)
      }
      startStream(s, ev.parent)
      if (s.turn && !ev.parent) s.turn.verb = VERBS[Math.floor(Math.random() * VERBS.length)]
      break
    }
    case 'thinking':
    case 'text': {
      const it = streamItem(s, ev.parent) ?? startStream(s, ev.parent)
      if (ev.event === 'text') it.text += ev.delta
      else {
        it.thinking += ev.delta
        const t = Date.now()
        it.thinkStart ??= t
        it.thinkEnd = t
      }
      if (s.turn) s.turn.chars += ev.delta.length
      break
    }
    case 'tool.preparing':
      s.preparing[ev.parent ?? ''] = ev.name
      break
    case 'assistant': {
      const it = streamItem(s, ev.parent)
      const list = container(s, ev.parent)
      if (it) {
        it.text = ev.text ?? it.text
        it.thinking = ev.thinking ?? it.thinking
        it.thinkingSeconds = ev.thinking_seconds ?? 0
        it.streaming = false
        if (!it.text.trim() && !it.thinking.trim()) removeItem(list, it.id)
        delete s.streams[ev.parent ?? '']
      } else if ((ev.text ?? '').trim() || (ev.thinking ?? '').trim()) {
        list.push({ kind: 'assistant', id: uid(), text: ev.text ?? '', thinking: ev.thinking ?? '', thinkingSeconds: ev.thinking_seconds ?? 0, streaming: false })
      }
      break
    }
    case 'tool.call': {
      const list = container(s, ev.parent)
      const pending = streamItem(s, ev.parent)
      if (pending && !pending.text.trim() && !pending.thinking.trim()) {
        removeItem(list, pending.id)
        delete s.streams[ev.parent ?? '']
      }
      list.push({
        kind: 'tool',
        id: uid(),
        callId: ev.call_id,
        name: ev.name,
        label: ev.label || ev.name,
        arg: ev.arg ?? '',
        args: ev.args ?? {},
        children: [],
        startedAt: Date.now()
      })
      s.preparing[ev.parent ?? ''] = null
      break
    }
    case 'permission.request': {
      if (!live) break
      const tool = findTool(s.items, ev.call_id)
      if (tool) tool.awaiting = true
      s.permissions.push({
        requestId: ev.request_id,
        callId: ev.call_id,
        name: ev.name,
        title: ev.title,
        question: ev.question,
        preview: ev.preview,
        previewKind: ev.preview_kind,
        alwaysLabel: ev.always_label
      })
      break
    }
    case 'plan.request':
      if (live) s.plan = { requestId: ev.request_id, plan: ev.plan }
      break
    case 'tool.result': {
      const tool = findTool(s.items, ev.call_id)
      if (!tool) break
      tool.result = { summary: ev.summary ?? '', detail: ev.detail ?? '', detailKind: ev.detail_kind ?? '', isError: Boolean(ev.is_error) }
      tool.endedAt = Date.now()
      tool.awaiting = false
      if (ev.detail_kind === 'todos') tool.todos = parseTodos(ev.detail) ?? tool.todos
      break
    }
    case 'todos': {
      s.todos = ev.todos
      const tool = lastTodoTool(s.items)
      if (tool) tool.todos = ev.todos
      else s.items.push({ kind: 'todos', id: uid(), todos: ev.todos })
      break
    }
    case 'info':
    case 'warn':
    case 'error':
      s.items.push({ kind: 'notice', id: uid(), level: ev.event, message: ev.message })
      break
    case 'compacted':
      finalizeStreams(s)
      s.items.push({ kind: 'compacted', id: uid(), before: ev.before, after: ev.after, limit: ev.limit, summary: ev.summary })
      break
    case 'interrupted':
      finalizeStreams(s)
      markStopped(s.items)
      s.items.push({ kind: 'interrupted', id: uid() })
      s.permissions = []
      s.plan = null
      break
    case 'turn.end':
      finalizeStreams(s)
      markStopped(s.items)
      s.permissions = []
      s.plan = null
      s.turn = null
      if (ev.checkpoint && !s.checkpoints.some((c) => c.turn_id === ev.checkpoint?.turn_id)) s.checkpoints.push(ev.checkpoint)
      break
    case 'session': {
      if (!s.session || s.session.id === ev.session.id) s.session = ev.session
      const i = s.sessions.findIndex((x) => x.id === ev.session.id)
      if (i >= 0) s.sessions[i] = ev.session
      else s.sessions.unshift(ev.session)
      break
    }
    default:
      break
  }
}

export function replay(s: State, transcript: EngineEvent[]): void {
  s.items = []
  s.streams = {}
  s.preparing = {}
  s.permissions = []
  s.plan = null
  for (const ev of transcript) applyEvent(s, ev, false)
  finalizeStreams(s)
  markStopped(s.items)
}

/** Before `initialize`: forget the previous engine's conversation (events that arrive while
 *  initializing, like a startup warning, belong to the new session). */
export function resetConversation(s: State): void {
  s.items = []
  s.streams = {}
  s.preparing = {}
  s.permissions = []
  s.plan = null
  s.todos = []
  s.checkpoints = []
  s.turn = null
}

export function applyInitialize(s: State, r: InitializeResult): void {
  s.version = r.version
  s.status = r.status
  s.effort = r.effort
  s.think = r.think
  s.session = r.session
  s.sessions = r.sessions.some((x) => x.id === r.session.id) ? r.sessions : [r.session, ...r.sessions]
  s.commands = r.commands
  s.mcp = r.mcp
  s.keys = r.keys
  s.phase = 'ready'
  s.restarting = null
}

let toastId = 0
export function toast(message: string, tone: Toast['tone'] = 'info'): void {
  const id = ++toastId
  setState((s) => {
    s.toasts.push({ id, message, tone })
  })
  setTimeout(() => setState((s) => void (s.toasts = s.toasts.filter((t) => t.id !== id))), tone === 'error' ? 6000 : 3500)
}
