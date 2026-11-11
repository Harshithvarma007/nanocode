// The API the preload script exposes as `window.nano`, shared by main, preload and renderer.

export type Theme = 'system' | 'light' | 'dark'

export interface AppSettings {
  /** Path to a `nanocode` executable, a python interpreter (run with -m nanocode), or a repo folder. */
  enginePath: string
  /** provider:model passed as --model when the engine starts. Empty = engine default. */
  defaultModel: string
  /** OLLAMA_HOST for the engine. Empty = engine default (http://localhost:11434). */
  ollamaHost: string
  theme: Theme
  effort: string
  think: boolean
}

export const KEY_ENVS = [
  { env: 'ANTHROPIC_API_KEY', provider: 'Anthropic', hint: 'sk-ant-…' },
  { env: 'OPENAI_API_KEY', provider: 'OpenAI', hint: 'sk-…' },
  { env: 'OPENROUTER_API_KEY', provider: 'OpenRouter', hint: 'sk-or-…' },
  { env: 'GEMINI_API_KEY', provider: 'Gemini', hint: 'AIza…' },
  { env: 'GROQ_API_KEY', provider: 'Groq', hint: 'gsk_…' }
] as const

export interface KeyHint {
  set: boolean
  /** Last four characters, for display only. */
  last4: string
}

export interface SettingsState {
  settings: AppSettings
  keys: Record<string, KeyHint>
  /** True when keys can be stored encrypted (Electron safeStorage). */
  secure: boolean
}

export interface Recent {
  path: string
  name: string
  opened: number
}

export interface WindowContext {
  project: string | null
  version: string
  isPackaged: boolean
  platform: string
  home: string
}

export type EngineState =
  | { state: 'idle' }
  | { state: 'starting'; command: string }
  | { state: 'running'; command: string; pid: number }
  | { state: 'restarting'; reason: string }
  | { state: 'exited'; code: number | null; signal: string | null; stderr: string[]; command: string }
  | { state: 'not-found'; searched: string[] }
  | { state: 'spawn-error'; message: string; command: string }

export type MenuAction =
  | 'new-session'
  | 'toggle-inspector'
  | 'toggle-sidebar'
  | 'settings'
  | 'shortcuts'
  | 'stop'
  | 'focus-composer'

export interface NanoApi {
  context(): Promise<WindowContext>
  pickFolder(): Promise<string | null>
  /** Open a project: 'bound' = this (welcome) window now shows it, 'opened' = in a new window,
   *  'focused' = it was already open in another window. */
  openProject(path: string): Promise<'bound' | 'opened' | 'focused'>
  recents(): Promise<Recent[]>
  removeRecent(path: string): Promise<Recent[]>
  getSettings(): Promise<SettingsState>
  setSettings(patch: Partial<AppSettings>): Promise<SettingsState>
  setKey(env: string, value: string | null): Promise<SettingsState>
  engineStart(): Promise<void>
  engineRestart(): Promise<void>
  engineSend(message: unknown): void
  pickEngine(): Promise<string | null>
  onEngineMessage(cb: (message: unknown) => void): () => void
  onEngineState(cb: (state: EngineState) => void): () => void
  onMenu(cb: (action: MenuAction) => void): () => void
  /** The main process bound this (welcome) window to a project, e.g. from File > Open Folder. */
  onProject(cb: (path: string) => void): () => void
  notify(title: string, body: string): void
  setTheme(theme: Theme): void
  setTitle(title: string): void
  pathForFile(file: File): string
  reveal(path: string): void
  openExternal(url: string): void
  openLogs(): void
}
