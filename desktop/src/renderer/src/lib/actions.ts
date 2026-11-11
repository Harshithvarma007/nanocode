// High-level actions: the engine connection lifecycle and everything the UI can ask for.
import type { EngineState } from '@shared/host'
import type {
  Checkpoint,
  ContextInfo,
  CostRow,
  EngineEvent,
  InitializeResult,
  McpServer,
  ModelOption,
  SessionInfo,
  Status,
  Todo
} from '@shared/protocol'
import { host } from './host'
import { engine } from './rpc'
import { applyEvent, applyInitialize, getState, replay, resetConversation, saveUi, setState, toast, type InspectorTab } from './store'

const errorMessage = (err: unknown) => (err instanceof Error ? err.message : String(err))

// ------------------------------------------------------------------ event pump

// Streaming deltas arrive many times a second; batch them so React renders ~30 times a second.
let queue: EngineEvent[] = []
let timer: ReturnType<typeof setTimeout> | null = null

function flush(): void {
  if (timer) clearTimeout(timer)
  timer = null
  if (!queue.length) return
  const batch = queue
  queue = []
  setState((s) => {
    for (const ev of batch) applyEvent(s, ev, true)
  })
}

function onEvent(ev: EngineEvent): void {
  if (ev.event === 'text' || ev.event === 'thinking') {
    queue.push(ev)
    timer ??= setTimeout(flush, 32)
    return
  }
  flush()
  if (ev.event === 'ready') {
    void initialize()
    return
  }
  setState((s) => applyEvent(s, ev, true))
  if (ev.event === 'turn.end') onTurnEnd(ev.checkpoint)
  if (ev.event === 'permission.request' && !document.hasFocus()) host.notify('nanocode needs your approval', ev.question || ev.title)
  if (ev.event === 'plan.request' && !document.hasFocus()) host.notify('nanocode has a plan', 'Review the plan to continue.')
  if (ev.event === 'session') syncTitle()
}

function onTurnEnd(checkpoint: Checkpoint | null): void {
  const { turn } = getState()
  void refreshCheckpoints()
  void refreshInspector()
  if (!document.hasFocus()) {
    const items = getState().items
    const last = [...items].reverse().find((i) => i.kind === 'assistant')
    const prompt = turn?.prompt ?? ''
    const body = last && last.kind === 'assistant' && last.text ? last.text.replace(/[#*`_>|]/g, '').slice(0, 140) : 'The turn is finished.'
    host.notify(prompt ? `Done: ${prompt.slice(0, 60)}` : 'nanocode finished', checkpoint ? `${body}\n${checkpoint.files.length} file(s) changed` : body)
  }
}

function syncTitle(): void {
  const { project, session } = getState()
  if (!project) return
  const name = project.split('/').filter(Boolean).pop() ?? project
  host.setTitle(session && session.title && session.title !== 'New session' ? `${name} — ${session.title}` : name)
}

// ------------------------------------------------------------------ lifecycle

let resumeSession: string | null = null
let wired = false

function wire(): void {
  if (wired) return
  wired = true
  engine.onEvent(onEvent)
  host.onEngineState(onEngineState)
  host.onProject((path) => void openProjectHere(path))
}

function onEngineState(state: EngineState): void {
  setState((s) => {
    s.engineState = state
  })
  switch (state.state) {
    case 'starting':
      if (getState().phase !== 'ready') setState((s) => void (s.phase = 'starting'))
      break
    case 'restarting': {
      const { session } = getState()
      resumeSession = session && session.turns > 0 ? session.id : null
      engine.rejectAll('The engine is restarting.')
      setState((s) => {
        s.restarting = state.reason
      })
      break
    }
    case 'not-found':
      engine.rejectAll('No engine.')
      setState((s) => void (s.phase = 'not-found'))
      break
    case 'exited':
    case 'spawn-error':
      engine.rejectAll('The engine stopped.')
      setState((s) => {
        s.phase = 'crashed'
        s.restarting = null
        s.turn = null
        s.permissions = []
        s.plan = null
      })
      break
    default:
      break
  }
}

export async function boot(): Promise<void> {
  wire()
  const ctx = await host.context()
  setState((s) => {
    s.ctx = ctx
  })
  void loadSettings()
  if (ctx.project) await openProjectHere(ctx.project)
  else setState((s) => void (s.phase = 'welcome'))
}

export async function openProjectHere(path: string): Promise<void> {
  setState((s) => {
    s.project = path
    s.phase = 'starting'
  })
  syncTitle()
  await host.engineStart()
}

export async function restartEngine(): Promise<void> {
  const { session } = getState()
  resumeSession = session && session.turns > 0 ? session.id : null
  setState((s) => {
    s.phase = 'starting'
  })
  await host.engineRestart()
}

async function initialize(): Promise<void> {
  try {
    setState(resetConversation)
    const r = await engine.request<InitializeResult>('initialize')
    setState((s) => applyInitialize(s, r))
    syncTitle()
    // The app's settings win over the engine's own defaults (env vars only reach a fresh spawn).
    const prefs = getState().settings?.settings
    if (prefs && prefs.think !== r.think) void setThink(prefs.think)
    if (prefs && prefs.effort && prefs.effort !== r.effort) void setEffort(prefs.effort)
    if (resumeSession && resumeSession !== r.session.id) {
      const id = resumeSession
      resumeSession = null
      await loadSession(id, true)
    }
    void refreshInspector()
  } catch (err) {
    toast(`Couldn't initialize the engine: ${errorMessage(err)}`, 'error')
  }
}

// ------------------------------------------------------------------ turns

const APP_COMMANDS: Record<string, () => void> = {
  clear: () => void newSession(),
  context: () => openInspector('context'),
  cost: () => openInspector('usage'),
  todos: () => openInspector('todos'),
  help: () => setState((s) => void (s.shortcutsOpen = true))
}

/** Send what the user typed. A few slash commands open native UI instead of terminal text. */
export async function sendPrompt(raw: string): Promise<boolean> {
  const text = raw.trim()
  if (!text) return false
  const { status, phase } = getState()
  if (phase !== 'ready') return false
  if (text.startsWith('/')) {
    const [name, ...rest] = text.slice(1).split(/\s+/)
    if (APP_COMMANDS[name] && !rest.join('').trim()) {
      APP_COMMANDS[name]()
      return true
    }
    if (name === 'model' && !rest.join('').trim()) {
      setState((s) => void (s.modelPickerOpen = true))
      return true
    }
  }
  if (status.busy) {
    toast('nanocode is still working. Press Stop (or Esc) first.')
    return false
  }
  try {
    await engine.request('prompt', { text })
    return true
  } catch (err) {
    toast(errorMessage(err), 'error')
    return false
  }
}

export async function interrupt(): Promise<void> {
  if (!getState().status.busy) return
  try {
    await engine.request('interrupt')
  } catch (err) {
    toast(errorMessage(err), 'error')
  }
}

export async function respondPermission(requestId: string, answer: 'yes' | 'always' | 'no', feedback?: string): Promise<void> {
  setState((s) => {
    s.permissions = s.permissions.filter((p) => p.requestId !== requestId)
  })
  try {
    await engine.request('permission.respond', { request_id: requestId, answer, ...(feedback ? { feedback } : {}) })
  } catch (err) {
    toast(errorMessage(err), 'error')
  }
}

export async function respondPlan(requestId: string, choice: 'accept-edits' | 'default' | 'no', feedback?: string): Promise<void> {
  setState((s) => {
    s.plan = null
  })
  try {
    await engine.request('plan.respond', { request_id: requestId, choice, ...(feedback ? { feedback } : {}) })
  } catch (err) {
    toast(errorMessage(err), 'error')
  }
}

// ------------------------------------------------------------------ modes and models

export async function cycleMode(): Promise<void> {
  if (getState().phase !== 'ready') return
  try {
    const r = await engine.request<{ status: Status }>('mode.cycle')
    setState((s) => void (s.status = r.status))
  } catch (err) {
    toast(errorMessage(err), 'error')
  }
}

export async function listModels(): Promise<{ current: string; options: ModelOption[] }> {
  return engine.request('models.list')
}

export async function setModel(spec: string): Promise<void> {
  const r = await engine.request<{ status: Status }>('model.set', { spec })
  setState((s) => void (s.status = r.status))
  toast(`Now using ${r.status.model}`)
}

export async function setEffort(effort: string): Promise<void> {
  setState((s) => void (s.effort = effort))
  if (getState().phase === 'ready') await engine.request('effort.set', { effort }).catch((e) => toast(errorMessage(e), 'error'))
}

export async function setThink(on: boolean): Promise<void> {
  setState((s) => void (s.think = on))
  if (getState().phase === 'ready') await engine.request('think.set', { on }).catch((e) => toast(errorMessage(e), 'error'))
}

export async function compact(instructions?: string): Promise<void> {
  try {
    await engine.request('compact', instructions ? { instructions } : {})
  } catch (err) {
    toast(errorMessage(err), 'error')
  }
}

// ------------------------------------------------------------------ sessions

export async function refreshSessions(): Promise<void> {
  try {
    const r = await engine.request<{ sessions: SessionInfo[] }>('session.list')
    setState((s) => {
      const open = s.session
      s.sessions = open && !r.sessions.some((x) => x.id === open.id) ? [open, ...r.sessions] : r.sessions
    })
  } catch {
    // keep the old list
  }
}

export async function newSession(): Promise<void> {
  const { status, phase, session } = getState()
  if (phase !== 'ready') return
  if (status.busy) return toast('Wait for the current turn to finish (or press Stop).')
  if (session && session.turns === 0 && !getState().items.length) {
    focusComposer()
    return
  }
  try {
    const r = await engine.request<{ session: SessionInfo }>('session.new')
    setState((s) => {
      s.session = r.session
      s.items = []
      s.todos = []
      s.checkpoints = []
      s.streams = {}
      s.permissions = []
      s.plan = null
      s.draft = ''
    })
    await refreshSessions()
    syncTitle()
    void refreshInspector()
    focusComposer()
  } catch (err) {
    toast(errorMessage(err), 'error')
  }
}

export async function loadSession(id: string, quiet = false): Promise<void> {
  const { status, session } = getState()
  if (session?.id === id && !quiet) return
  if (status.busy) return toast("Can't switch sessions while nanocode is working.")
  try {
    const r = await engine.request<{ session: SessionInfo; transcript: EngineEvent[]; todos: Todo[]; status: Status }>('session.load', { session_id: id })
    setState((s) => {
      s.session = r.session
      s.status = r.status
      s.todos = r.todos ?? []
      s.checkpoints = []
      replay(s, r.transcript ?? [])
    })
    syncTitle()
    void refreshCheckpoints()
    void refreshInspector()
  } catch (err) {
    if (!quiet) toast(errorMessage(err), 'error')
  }
}

export async function renameSession(id: string, title: string): Promise<void> {
  const clean = title.trim()
  if (!clean) return
  try {
    const r = await engine.request<{ session: SessionInfo }>('session.rename', { session_id: id, title: clean })
    setState((s) => {
      const i = s.sessions.findIndex((x) => x.id === id)
      if (i >= 0) s.sessions[i] = r.session
      if (s.session?.id === id) s.session = r.session
    })
    syncTitle()
  } catch (err) {
    toast(errorMessage(err), 'error')
  }
}

export async function deleteSession(id: string): Promise<void> {
  try {
    await engine.request('session.delete', { session_id: id })
    setState((s) => {
      s.sessions = s.sessions.filter((x) => x.id !== id)
    })
  } catch (err) {
    toast(errorMessage(err), 'error')
  }
}

// ------------------------------------------------------------------ inspector data

export async function refreshCheckpoints(): Promise<void> {
  try {
    const r = await engine.request<{ checkpoints: Checkpoint[] }>('checkpoint.list')
    setState((s) => void (s.checkpoints = r.checkpoints))
  } catch {
    // older engines may not support checkpoints
  }
}

export async function refreshContext(): Promise<ContextInfo | null> {
  try {
    const r = await engine.request<ContextInfo>('context.get')
    setState((s) => void (s.context = r))
    return r
  } catch {
    return null
  }
}

export async function refreshInspector(): Promise<void> {
  if (getState().phase !== 'ready') return
  const tasks: Promise<unknown>[] = [refreshContext()]
  tasks.push(
    engine
      .request<{ rows: CostRow[] }>('cost.get')
      .then((r) => setState((s) => void (s.cost = r.rows)))
      .catch(() => {})
  )
  tasks.push(
    engine
      .request<{ servers: McpServer[] }>('mcp.list')
      .then((r) => setState((s) => void (s.mcp = r.servers)))
      .catch(() => {})
  )
  tasks.push(
    engine
      .request<{ todos: Todo[] }>('todos.get')
      .then((r) => setState((s) => void (s.todos = r.todos)))
      .catch(() => {})
  )
  await Promise.all(tasks)
}

export async function restoreCheckpoint(turnId: string, conversation: boolean): Promise<void> {
  try {
    const r = await engine.request<{ restored: string[]; transcript?: EngineEvent[]; status: Status }>('checkpoint.restore', {
      turn_id: turnId,
      conversation
    })
    setState((s) => {
      s.status = r.status
      if (conversation && r.transcript) replay(s, r.transcript)
      s.rewindTarget = null
    })
    if (conversation) {
      const cp = getState().checkpoints.find((c) => c.turn_id === turnId)
      if (cp) setState((s) => void (s.draft = cp.prompt))
    }
    await refreshCheckpoints()
    void refreshInspector()
    const n = r.restored.length
    toast(n ? `Restored ${n} file${n === 1 ? '' : 's'}${conversation ? ' and rewound the conversation' : ''}` : 'Nothing to restore')
  } catch (err) {
    toast(errorMessage(err), 'error')
  }
}

export async function searchFiles(query: string): Promise<string[]> {
  try {
    const r = await engine.request<{ files: string[] }>('files.search', { query, limit: 30 })
    return r.files
  } catch {
    return []
  }
}

// ------------------------------------------------------------------ settings

export async function loadSettings(): Promise<void> {
  const settings = await host.getSettings()
  setState((s) => void (s.settings = settings))
  host.setTheme(settings.settings.theme)
}

// ------------------------------------------------------------------ UI

export function openInspector(tab?: InspectorTab): void {
  setState((s) => {
    s.inspectorOpen = true
    if (tab) s.inspectorTab = tab
  })
  saveUi('inspectorOpen', true)
  if (tab) saveUi('inspectorTab', tab)
  void refreshInspector()
}

export function toggleInspector(): void {
  const open = !getState().inspectorOpen
  setState((s) => void (s.inspectorOpen = open))
  saveUi('inspectorOpen', open)
  if (open) void refreshInspector()
}

export function toggleSidebar(): void {
  const open = !getState().sidebarOpen
  setState((s) => void (s.sidebarOpen = open))
  saveUi('sidebarOpen', open)
}

export function setInspectorTab(tab: InspectorTab): void {
  setState((s) => void (s.inspectorTab = tab))
  saveUi('inspectorTab', tab)
  void refreshInspector()
}

export function focusComposer(): void {
  requestAnimationFrame(() => document.querySelector<HTMLTextAreaElement>('[data-composer]')?.focus())
}
