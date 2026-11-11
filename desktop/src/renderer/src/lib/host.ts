// The renderer talks to "the host" through one interface. In Electron that's `window.nano`
// (the preload script). In a plain browser it's BrowserHost, which reaches the engine through
// dev/ws-bridge.mjs and keeps settings in localStorage. API keys are never stored by the
// browser host: they live in memory for this tab and are passed to the bridge on restart.
import {
  KEY_ENVS,
  type AppSettings,
  type EngineState,
  type KeyHint,
  type MenuAction,
  type NanoApi,
  type Recent,
  type SettingsState,
  type Theme,
  type WindowContext
} from '@shared/host'

type Listener<T> = (value: T) => void

function emitter<T>() {
  const listeners = new Set<Listener<T>>()
  return {
    on(cb: Listener<T>) {
      listeners.add(cb)
      return () => void listeners.delete(cb)
    },
    emit(value: T) {
      for (const cb of listeners) cb(value)
    }
  }
}

const DEFAULT_SETTINGS: AppSettings = {
  enginePath: '',
  defaultModel: '',
  ollamaHost: '',
  theme: 'system',
  effort: 'high',
  think: true
}

function load<T>(key: string, fallback: T): T {
  try {
    const raw = localStorage.getItem(key)
    return raw ? (JSON.parse(raw) as T) : fallback
  } catch {
    return fallback
  }
}

function save(key: string, value: unknown): void {
  try {
    localStorage.setItem(key, JSON.stringify(value))
  } catch {
    // storage can be unavailable; settings then last for this tab only
  }
}

class BrowserHost implements NanoApi {
  private params = new URLSearchParams(location.search)
  private ws: WebSocket | null = null
  private closing = false
  private messages = emitter<unknown>()
  private states = emitter<EngineState>()
  private menus = emitter<MenuAction>()
  private keys = new Map<string, string>()

  private get bridge(): string {
    return this.params.get('bridge') ?? 'ws://localhost:8787'
  }

  async context(): Promise<WindowContext> {
    return { project: this.params.get('cwd'), version: 'dev', isPackaged: false, platform: 'browser', home: '' }
  }

  async pickFolder(): Promise<string | null> {
    return null // the welcome screen shows a path field instead
  }

  async openProject(path: string): Promise<'bound' | 'opened' | 'focused'> {
    const recents = load<Recent[]>('nano.recents', []).filter((r) => r.path !== path)
    recents.unshift({ path, name: path.split('/').filter(Boolean).pop() ?? path, opened: Date.now() })
    save('nano.recents', recents.slice(0, 12))
    const next = new URLSearchParams(this.params)
    next.set('cwd', path)
    next.set('bridge', this.bridge)
    location.search = next.toString()
    return 'opened'
  }

  async recents(): Promise<Recent[]> {
    return load<Recent[]>('nano.recents', [])
  }

  async removeRecent(path: string): Promise<Recent[]> {
    const list = load<Recent[]>('nano.recents', []).filter((r) => r.path !== path)
    save('nano.recents', list)
    return list
  }

  private state(): SettingsState {
    const keys: Record<string, KeyHint> = {}
    for (const { env } of KEY_ENVS) {
      const value = this.keys.get(env)
      keys[env] = { set: Boolean(value), last4: value ? value.slice(-4) : '' }
    }
    return { settings: { ...DEFAULT_SETTINGS, ...load<Partial<AppSettings>>('nano.settings', {}) }, keys, secure: false }
  }

  async getSettings(): Promise<SettingsState> {
    return this.state()
  }

  async setSettings(patch: Partial<AppSettings>): Promise<SettingsState> {
    const before = this.state().settings
    save('nano.settings', { ...before, ...patch })
    if (patch.ollamaHost !== undefined && patch.ollamaHost !== before.ollamaHost) void this.engineRestart('Ollama host changed')
    return this.state()
  }

  async setKey(env: string, value: string | null): Promise<SettingsState> {
    if (value?.trim()) this.keys.set(env, value.trim())
    else this.keys.delete(env)
    void this.engineRestart('API keys changed')
    return this.state()
  }

  private env(): Record<string, string> {
    const env: Record<string, string> = Object.fromEntries(this.keys)
    const s = this.state().settings
    if (s.ollamaHost) env.OLLAMA_HOST = s.ollamaHost
    if (!s.think) env.NANOCODE_THINK = '0'
    env.NANOCODE_EFFORT = s.effort
    return env
  }

  async engineStart(): Promise<void> {
    const cwd = this.params.get('cwd')
    if (!cwd) return
    this.closing = true
    this.ws?.close()
    this.closing = false
    const url = new URL(this.bridge)
    url.searchParams.set('cwd', cwd)
    const engine = this.params.get('engine')
    if (engine) url.searchParams.set('engine', engine)
    const model = this.params.get('model') || this.state().settings.defaultModel
    if (model) url.searchParams.set('model', model)
    this.states.emit({ state: 'starting', command: `bridge ${this.bridge}` })
    const ws = new WebSocket(url)
    this.ws = ws
    ws.onmessage = (e) => {
      let msg: Record<string, unknown>
      try {
        msg = JSON.parse(String(e.data))
      } catch {
        return
      }
      if (msg && typeof msg === 'object' && '$bridge' in msg) {
        const { $bridge: _kind, ...state } = msg
        this.states.emit(state as unknown as EngineState)
      } else this.messages.emit(msg)
    }
    ws.onclose = () => {
      if (this.ws !== ws || this.closing) return
      this.states.emit({
        state: 'exited',
        code: null,
        signal: null,
        stderr: [`Lost the connection to the dev bridge at ${this.bridge}.`, 'Start it with: npm run web (or npm run bridge).'],
        command: `bridge ${this.bridge}`
      })
    }
    // Apply keys/settings that were set before this connection.
    if (this.keys.size) ws.addEventListener('open', () => ws.send(JSON.stringify({ $bridge: 'restart', env: this.env() })), { once: true })
  }

  async engineRestart(reason = 'Restarting'): Promise<void> {
    if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify({ $bridge: 'restart', env: this.env(), reason }))
    else await this.engineStart()
  }

  engineSend(message: unknown): void {
    if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify(message))
  }

  async pickEngine(): Promise<string | null> {
    return null
  }

  onEngineMessage(cb: (message: unknown) => void) {
    return this.messages.on(cb)
  }
  onEngineState(cb: (state: EngineState) => void) {
    return this.states.on(cb)
  }
  onMenu(cb: (action: MenuAction) => void) {
    return this.menus.on(cb)
  }
  onProject(_cb: (path: string) => void) {
    return () => {}
  }
  notify(title: string, body: string): void {
    if ('Notification' in window && Notification.permission === 'granted') new Notification(title, { body })
  }
  setTheme(_theme: Theme): void {}
  setTitle(title: string): void {
    document.title = title
  }
  pathForFile(_file: File): string {
    return ''
  }
  reveal(_path: string): void {}
  openExternal(url: string): void {
    window.open(url, '_blank', 'noopener')
  }
  openLogs(): void {}
}

export const host: NanoApi = window.nano ?? new BrowserHost()
export const isElectron = Boolean(window.nano)
