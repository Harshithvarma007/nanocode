// Full-window screens: the welcome / project picker, engine starting, engine not found, engine crashed.
import { useEffect, useState } from 'react'
import { CircleAlert, Folder, FolderOpen, RefreshCw, ScrollText, Settings, X } from 'lucide-react'
import type { Recent } from '@shared/host'
import { openProjectHere, restartEngine } from '../lib/actions'
import { relativeTime, tildify } from '../lib/format'
import { host, isElectron } from '../lib/host'
import { setState, useStore } from '../lib/store'
import { Glyph, Mark } from './ui'

async function openPath(path: string) {
  const result = await host.openProject(path)
  if (result === 'bound') await openProjectHere(path)
}

export function Welcome() {
  const [recents, setRecents] = useState<Recent[]>([])
  const [path, setPath] = useState('')
  const home = useStore((s) => s.ctx?.home ?? '')
  useEffect(() => {
    void host.recents().then(setRecents)
  }, [])
  const pick = async () => {
    const dir = await host.pickFolder()
    if (dir) await openPath(dir)
  }
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.metaKey && e.key.toLowerCase() === 'o' && !isElectron) {
        e.preventDefault()
        document.querySelector<HTMLInputElement>('[data-path-input]')?.focus()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])
  return (
    <div className="screen">
      <div className="screen-top drag" />
      <div className="screen-center">
        <div className="welcome">
          <div className="welcome-hero">
            <div className="app-mark">
              <Mark size={40} strokeWidth={2.3} />
            </div>
            <h1>nanocode</h1>
            <p>A coding agent you can read in an afternoon. Open a project to start.</p>
          </div>
          {isElectron ? (
            <div className="welcome-actions">
              <button className="btn primary lg" onClick={() => void pick()}>
                <FolderOpen size={16} /> Open Folder…
              </button>
              <button className="btn lg" onClick={() => setState((s) => void (s.settingsOpen = true))}>
                <Settings size={15} /> Settings
              </button>
            </div>
          ) : (
            <form
              className="path-form"
              onSubmit={(e) => {
                e.preventDefault()
                if (path.trim()) void openPath(path.trim())
              }}
            >
              <input data-path-input className="input mono" style={{ height: 38 }} placeholder="/path/to/project" value={path} onChange={(e) => setPath(e.target.value)} />
              <button className="btn primary lg" type="submit">
                <FolderOpen size={16} /> Open
              </button>
            </form>
          )}
          <div className="recents-title">
            <span>Recent projects</span>
            {isElectron && <span style={{ textTransform: 'none', letterSpacing: 0, fontWeight: 500 }}>⌘O to open a folder</span>}
          </div>
          <div className="recents">
            {recents.length === 0 && <div className="recents-empty">Projects you open will show up here.</div>}
            {recents.map((r) => (
              <div key={r.path} className="recent" role="button" tabIndex={0} onClick={() => void openPath(r.path)} onKeyDown={(e) => e.key === 'Enter' && void openPath(r.path)}>
                <div className="recent-icon">
                  <Folder size={16} />
                </div>
                <div className="recent-body">
                  <div className="recent-name">{r.name}</div>
                  <div className="recent-path">
                    <bdi>{tildify(r.path, home)}</bdi>
                  </div>
                </div>
                <div className="recent-when">{relativeTime(r.opened / 1000)}</div>
                <button
                  className="icon-btn sm"
                  title="Remove from recents"
                  onClick={(e) => {
                    e.stopPropagation()
                    void host.removeRecent(r.path).then(setRecents)
                  }}
                >
                  <X size={13} />
                </button>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  )
}

export function Connecting() {
  const restarting = useStore((s) => s.restarting)
  const [slow, setSlow] = useState(false)
  useEffect(() => {
    const t = setTimeout(() => setSlow(true), 6000)
    return () => clearTimeout(t)
  }, [])
  return (
    <div className="screen-center" style={{ height: '100%' }}>
      <div className="connecting">
        <Glyph />
        <div>{restarting ? `${restarting}. Restarting the engine…` : 'Starting nanocode…'}</div>
        {slow && <div style={{ fontSize: 12 }}>MCP servers and models can take a few seconds to start.</div>}
      </div>
    </div>
  )
}

export function EngineNotFound() {
  const state = useStore((s) => s.engineState)
  const searched = state.state === 'not-found' ? state.searched : []
  return (
    <div className="screen-center" style={{ height: '100%' }}>
      <div className="problem">
        <div className="problem-icon neutral">
          <Mark size={24} />
        </div>
        <h1>nanocode isn't installed yet</h1>
        <p>The desktop app drives the nanocode engine, a small Python program. Install it once, then try again.</p>
        <ol className="steps">
          <li>
            Install nanocode from its folder (needs Python 3.10+ and uv or pip):
            <code>uv tool install -e /path/to/nanocode</code>
          </li>
          <li>
            Or with pip, in a virtualenv:
            <code>python3 -m venv .venv && .venv/bin/pip install -e .</code>
          </li>
          <li>Then press Try again. If it's somewhere unusual, choose it in Settings → Engine.</li>
        </ol>
        {searched.length > 0 && (
          <details className="searched">
            <summary>Where the app looked</summary>
            <ul className="selectable">
              {searched.map((s) => (
                <li key={s}>{s}</li>
              ))}
            </ul>
          </details>
        )}
        <div className="row">
          <button className="btn primary lg" onClick={() => void restartEngine()}>
            <RefreshCw size={15} /> Try again
          </button>
          {isElectron && (
            <button className="btn lg" onClick={() => void host.pickEngine()}>
              <FolderOpen size={15} /> Choose engine…
            </button>
          )}
        </div>
      </div>
    </div>
  )
}

export function EngineCrashed() {
  const state = useStore((s) => s.engineState)
  const stderr = state.state === 'exited' ? state.stderr : []
  const message = state.state === 'spawn-error' ? state.message : ''
  const code = state.state === 'exited' ? (state.signal ? `killed by ${state.signal}` : state.code !== null ? `exit code ${state.code}` : null) : null
  const text = stderr.join('\n')
  const tooOld = /invalid choice: 'serve'|unrecognized arguments|No module named nanocode/.test(text)
  return (
    <div className="screen-center" style={{ height: '100%' }}>
      <div className="problem">
        <div className="problem-icon">
          <CircleAlert size={24} />
        </div>
        <h1>The nanocode engine stopped</h1>
        <p>
          {tooOld
            ? "This nanocode doesn't have the `serve` command the desktop app needs. Update nanocode (pull the latest version and reinstall), or point Settings → Engine at a newer one."
            : `It exited unexpectedly${code ? ` (${code})` : ''}. Your sessions are saved, so restarting picks up where you left off.`}
        </p>
        {(text || message) && <pre className="selectable">{message || text}</pre>}
        <div className="row">
          <button className="btn primary lg" onClick={() => void restartEngine()}>
            <RefreshCw size={15} /> Restart engine
          </button>
          <button className="btn lg" onClick={() => setState((s) => void (s.settingsOpen = true))}>
            <Settings size={15} /> Settings
          </button>
          {isElectron && (
            <button className="btn lg ghost" onClick={() => host.openLogs()}>
              <ScrollText size={15} /> Logs
            </button>
          )}
        </div>
      </div>
    </div>
  )
}

export function Shortcuts({ onClose }: { onClose: () => void }) {
  const commands = useStore((s) => s.commands)
  const rows: [string, string[]][] = [
    ['Send message', ['↩']],
    ['New line', ['⇧', '↩']],
    ['Cycle mode (default → accept edits → plan)', ['⇧', '⇥']],
    ['Stop nanocode', ['esc']],
    ['New session', ['⌘', 'N']],
    ['Open folder', ['⌘', 'O']],
    ['Toggle sidebar', ['⌘', '\\']],
    ['Toggle inspector', ['⌥', '⌘', 'I']],
    ['Focus message box', ['⌘', 'L']],
    ['Settings', ['⌘', ',']],
    ['Approve / always / deny a request', ['1', '2', '3']]
  ]
  return (
    <div className="overlay" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="dialog wide">
        <div className="dialog-head">
          <h2>Keyboard shortcuts</h2>
          <button className="icon-btn" onClick={onClose}>
            <X size={15} />
          </button>
        </div>
        <div className="dialog-body">
          <div className="shortcuts">
            {rows.map(([label, keys]) => (
              <div key={label} style={{ display: 'contents' }}>
                <span>{label}</span>
                <span className="keys">
                  {keys.map((k) => (
                    <kbd key={k}>{k}</kbd>
                  ))}
                </span>
              </div>
            ))}
            <h4>In the message box</h4>
            <span>
              <code>@path</code> attach a file · <code>!cmd</code> run a shell command yourself · <code># note</code> save to AGENTS.md
            </span>
            <span />
            {commands.length > 0 && <h4>Commands</h4>}
            {commands.map((c) => (
              <div key={c.name} style={{ display: 'contents' }}>
                <span style={{ color: 'var(--text-2)' }}>{c.description}</span>
                <span className="keys">
                  <code>/{c.name}</code>
                </span>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  )
}
