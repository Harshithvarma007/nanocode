import { useEffect, useState } from 'react'
import { Cpu, FolderOpen, Info, KeyRound, Palette, RefreshCw, ScrollText, Settings as SettingsIcon, ShieldCheck, TriangleAlert, X } from 'lucide-react'
import { KEY_ENVS, type AppSettings, type Theme } from '@shared/host'
import type { ModelOption } from '@shared/protocol'
import { listModels, loadSettings, restartEngine, setEffort, setModel, setThink } from '../lib/actions'
import { host, isElectron } from '../lib/host'
import { setState, toast, useStore } from '../lib/store'

type Section = 'general' | 'keys' | 'engine'

async function save(patch: Partial<AppSettings>) {
  const next = await host.setSettings(patch)
  setState((s) => void (s.settings = next))
  if (patch.theme) {
    host.setTheme(patch.theme)
    applyTheme(patch.theme)
  }
}

export function applyTheme(theme: Theme) {
  const root = document.documentElement
  if (theme === 'system') root.removeAttribute('data-theme')
  else root.setAttribute('data-theme', theme)
}

function Field({ label, hint, children }: { label: string; hint?: React.ReactNode; children: React.ReactNode }) {
  return (
    <div className="field">
      <div className="field-label">
        <div>{label}</div>
        {hint && <div>{hint}</div>}
      </div>
      <div className="field-control">{children}</div>
    </div>
  )
}

function Segmented<T extends string>({ value, options, onChange }: { value: T; options: { value: T; label: string }[]; onChange: (v: T) => void }) {
  return (
    <div className="segmented">
      {options.map((o) => (
        <button key={o.value} className={o.value === value ? 'active' : ''} onClick={() => onChange(o.value)}>
          {o.label}
        </button>
      ))}
    </div>
  )
}

function General() {
  const settings = useStore((s) => s.settings?.settings)
  const effort = useStore((s) => s.effort)
  const think = useStore((s) => s.think)
  const ready = useStore((s) => s.phase === 'ready')
  const [models, setModels] = useState<ModelOption[]>([])
  const [model, setModelText] = useState(settings?.defaultModel ?? '')
  useEffect(() => {
    if (ready) listModels().then((r) => setModels(r.options)).catch(() => {})
  }, [ready])
  useEffect(() => setModelText(settings?.defaultModel ?? ''), [settings?.defaultModel])
  if (!settings) return null
  return (
    <>
      <h3>Appearance</h3>
      <Field label="Theme" hint="Follows macOS unless you pick one.">
        <Segmented<Theme>
          value={settings.theme}
          options={[
            { value: 'system', label: 'System' },
            { value: 'light', label: 'Light' },
            { value: 'dark', label: 'Dark' }
          ]}
          onChange={(theme) => void save({ theme })}
        />
      </Field>
      <h3>Model</h3>
      <Field label="Default model" hint="Used when the engine starts. Leave empty to let nanocode pick.">
        <input
          className="input mono"
          list="nano-models"
          placeholder="auto"
          value={model}
          onChange={(e) => setModelText(e.target.value)}
          onBlur={() => model !== settings.defaultModel && void save({ defaultModel: model.trim() })}
          onKeyDown={(e) => e.key === 'Enter' && (e.target as HTMLInputElement).blur()}
        />
        <datalist id="nano-models">
          {models.map((m) => (
            <option key={m.spec} value={m.spec}>
              {m.label}
            </option>
          ))}
        </datalist>
      </Field>
      {model.trim() && ready && (
        <div style={{ display: 'flex', justifyContent: 'flex-end', margin: '-4px 0 6px' }}>
          <button
            className="btn sm"
            onClick={() =>
              void setModel(model.trim()).catch((e) => toast(e instanceof Error ? e.message : String(e), 'error'))
            }
          >
            Use it now
          </button>
        </div>
      )}
      <Field label="Reasoning effort" hint="For Claude models.">
        <Segmented
          value={effort}
          options={['low', 'medium', 'high', 'xhigh', 'max'].map((v) => ({ value: v, label: v }))}
          onChange={(v) => {
            void setEffort(v)
            void save({ effort: v })
          }}
        />
      </Field>
      <Field label="Thinking" hint="Lets local models reason before answering (slower), and shows thinking in the transcript.">
        <button
          className={`switch ${think ? 'on' : ''}`}
          role="switch"
          aria-checked={think}
          onClick={() => {
            void setThink(!think)
            void save({ think: !think })
          }}
        />
      </Field>
    </>
  )
}

function KeyRow({ env, provider, placeholder }: { env: string; provider: string; placeholder: string }) {
  const hint = useStore((s) => s.settings?.keys[env])
  const engineSees = useStore((s) => s.keys[env])
  const [editing, setEditing] = useState(false)
  const [value, setValue] = useState('')
  const [busy, setBusy] = useState(false)
  const commit = async (next: string | null) => {
    setBusy(true)
    try {
      const state = await host.setKey(env, next)
      setState((s) => void (s.settings = state))
      toast(next ? `${provider} key saved. Restarting the engine…` : `${provider} key removed. Restarting the engine…`)
      setEditing(false)
      setValue('')
    } catch (e) {
      toast(e instanceof Error ? e.message : String(e), 'error')
    } finally {
      setBusy(false)
    }
  }
  return (
    <div className="key-row">
      <div className="key-provider">
        {provider}
        <div className="key-env">{env}</div>
      </div>
      {editing ? (
        <>
          <input
            className="input mono"
            type="password"
            autoFocus
            placeholder={placeholder}
            value={value}
            onChange={(e) => setValue(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && value.trim()) void commit(value)
              if (e.key === 'Escape') {
                e.stopPropagation()
                setEditing(false)
              }
            }}
          />
          <button className="btn primary sm" disabled={!value.trim() || busy} onClick={() => void commit(value)}>
            Save
          </button>
          <button className="btn ghost sm" onClick={() => setEditing(false)}>
            Cancel
          </button>
        </>
      ) : (
        <>
          <div className={`key-value ${hint?.set ? '' : 'unset'}`}>
            {hint?.set ? `•••• •••• ${hint.last4}` : engineSees ? 'Set in your environment or .env' : 'Not set'}
          </div>
          <button className="btn sm" onClick={() => setEditing(true)}>
            {hint?.set ? 'Replace' : 'Add key'}
          </button>
          {hint?.set && (
            <button className="icon-btn sm" title="Remove" disabled={busy} onClick={() => void commit(null)}>
              <X size={13} />
            </button>
          )}
        </>
      )}
    </div>
  )
}

function Keys() {
  const secure = useStore((s) => s.settings?.secure)
  return (
    <>
      <h3>API keys</h3>
      <p className="sub">Keys are passed to the engine as environment variables when it starts. Saving one restarts the engine; your conversation is kept.</p>
      {isElectron ? (
        <div className="callout">
          <ShieldCheck size={15} />
          <span>{secure ? 'Stored encrypted with the macOS Keychain (Electron safeStorage). Only the last four characters are ever shown.' : 'Secure storage is not available, so keys cannot be saved.'}</span>
        </div>
      ) : (
        <div className="callout warn">
          <TriangleAlert size={15} />
          <span>Browser dev mode: keys are kept in memory for this tab only and sent to the local dev bridge. The desktop app stores them encrypted.</span>
        </div>
      )}
      <div>
        {KEY_ENVS.map((k) => (
          <KeyRow key={k.env} env={k.env} provider={k.provider} placeholder={k.hint} />
        ))}
      </div>
    </>
  )
}

function Engine() {
  const settings = useStore((s) => s.settings?.settings)
  const engineState = useStore((s) => s.engineState)
  const version = useStore((s) => s.version)
  const [ollama, setOllama] = useState(settings?.ollamaHost ?? '')
  const [enginePath, setEnginePath] = useState(settings?.enginePath ?? '')
  useEffect(() => setEnginePath(settings?.enginePath ?? ''), [settings?.enginePath])
  if (!settings) return null
  const command = 'command' in engineState ? engineState.command : ''
  return (
    <>
      <h3>Engine</h3>
      <p className="sub">The app runs <code>nanocode serve</code> for each project window and talks to it over stdio.</p>
      <div className="callout">
        <Info size={15} />
        <span className="selectable" style={{ wordBreak: 'break-all' }}>
          {engineState.state === 'running' ? (
            <>
              Running nanocode {version} (pid {engineState.pid})
              <br />
              <code style={{ fontSize: 11.5 }}>{command}</code>
            </>
          ) : (
            `Engine: ${engineState.state}`
          )}
        </span>
      </div>
      <Field label="Engine path" hint="A nanocode executable, a Python with nanocode installed, or a checkout folder. Empty = find it automatically.">
        <input
          className="input mono"
          placeholder="automatic"
          value={enginePath}
          onChange={(e) => setEnginePath(e.target.value)}
          onBlur={() => enginePath !== settings.enginePath && void save({ enginePath: enginePath.trim() })}
          onKeyDown={(e) => e.key === 'Enter' && (e.target as HTMLInputElement).blur()}
        />
        {isElectron && (
          <button className="icon-btn" title="Choose…" onClick={() => void host.pickEngine().then(() => loadSettings())}>
            <FolderOpen size={15} />
          </button>
        )}
      </Field>
      <Field label="Ollama host" hint="Where local models run. Changing it restarts the engine.">
        <input
          className="input mono"
          placeholder="http://localhost:11434"
          value={ollama}
          onChange={(e) => setOllama(e.target.value)}
          onBlur={() => ollama !== settings.ollamaHost && void save({ ollamaHost: ollama.trim() })}
          onKeyDown={(e) => e.key === 'Enter' && (e.target as HTMLInputElement).blur()}
        />
      </Field>
      <div className="row" style={{ marginTop: 14 }}>
        <button className="btn" onClick={() => void restartEngine()}>
          <RefreshCw size={13} /> Restart engine
        </button>
        {isElectron && (
          <button className="btn" onClick={() => host.openLogs()}>
            <ScrollText size={13} /> Open logs
          </button>
        )}
      </div>
    </>
  )
}

export function SettingsDialog() {
  const [section, setSection] = useState<Section>('general')
  const close = () => setState((s) => void (s.settingsOpen = false))
  useEffect(() => {
    void loadSettings()
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') close()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])
  const nav: { id: Section; label: string; icon: React.ReactNode }[] = [
    { id: 'general', label: 'General', icon: <Palette size={14} /> },
    { id: 'keys', label: 'API keys', icon: <KeyRound size={14} /> },
    { id: 'engine', label: 'Engine', icon: <Cpu size={14} /> }
  ]
  return (
    <div className="overlay" onMouseDown={(e) => e.target === e.currentTarget && close()}>
      <div className="dialog settings" role="dialog" aria-label="Settings">
        <div className="dialog-head" style={{ paddingBottom: 12, borderBottom: '1px solid var(--border)' }}>
          <SettingsIcon size={17} style={{ color: 'var(--text-3)' }} />
          <h2>Settings</h2>
          <button className="icon-btn" onClick={close} title="Close (Esc)">
            <X size={15} />
          </button>
        </div>
        <div className="settings-body">
          <nav className="settings-nav">
            {nav.map((n) => (
              <button key={n.id} className={section === n.id ? 'active' : ''} onClick={() => setSection(n.id)}>
                {n.icon}
                {n.label}
              </button>
            ))}
          </nav>
          <div className="settings-pane">
            {section === 'general' && <General />}
            {section === 'keys' && <Keys />}
            {section === 'engine' && <Engine />}
          </div>
        </div>
      </div>
    </div>
  )
}
