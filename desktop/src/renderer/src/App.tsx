import { useEffect, useState } from 'react'
import { PanelLeft, RefreshCw } from 'lucide-react'
import type { MenuAction } from '@shared/host'
import { cycleMode, focusComposer, interrupt, newSession, toggleInspector, toggleSidebar } from './lib/actions'
import { host, isElectron } from './lib/host'
import { modelName, plural } from './lib/format'
import { setState, useStore } from './lib/store'
import { Composer } from './components/Composer'
import { Inspector, RewindDialog } from './components/Inspector'
import { Connecting, EngineCrashed, EngineNotFound, Shortcuts, Welcome } from './components/Screens'
import { applyTheme, SettingsDialog } from './components/Settings'
import { Sidebar } from './components/Sidebar'
import { Transcript } from './components/Transcript'

function MainHeader({ scrolled }: { scrolled: boolean }) {
  const session = useStore((s) => s.session)
  const sidebarOpen = useStore((s) => s.sidebarOpen)
  const inspectorOpen = useStore((s) => s.inspectorOpen)
  const model = useStore((s) => s.status.model)
  const title = session && session.title !== 'New session' ? session.title : 'New session'
  return (
    <header className={`main-header drag ${scrolled ? 'scrolled' : ''} ${sidebarOpen ? '' : 'with-lights'}`}>
      {!sidebarOpen && (
        <button className="icon-btn" title="Show sidebar (⌘\)" onClick={toggleSidebar}>
          <PanelLeft size={16} />
        </button>
      )}
      <div className="main-title">
        <span className="main-title-text">{title}</span>
        {session && session.turns > 0 && (
          <span className="main-title-sub">
            {plural(session.turns, 'turn')} · {modelName(model)}
          </span>
        )}
      </div>
      {!inspectorOpen && (
        <button className="icon-btn" title="Show inspector (⌥⌘I)" onClick={toggleInspector}>
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round">
            <rect x="3" y="3" width="18" height="18" rx="2" />
            <path d="M15 3v18" />
          </svg>
        </button>
      )}
    </header>
  )
}

function ProjectView() {
  const phase = useStore((s) => s.phase)
  const restarting = useStore((s) => s.restarting)
  const [scrolled, setScrolled] = useState(false)
  return (
    <div className="app">
      <Sidebar />
      <main className="main">
        <MainHeader scrolled={scrolled} />
        {phase === 'ready' ? (
          <>
            {restarting && (
              <div className="banner">
                <RefreshCw size={13} style={{ animation: 'spin 1s linear infinite' }} />
                {restarting}. Restarting the engine…
              </div>
            )}
            <Transcript onScrolled={setScrolled} />
            <Composer />
          </>
        ) : phase === 'not-found' ? (
          <EngineNotFound />
        ) : phase === 'crashed' ? (
          <EngineCrashed />
        ) : (
          <Connecting />
        )}
      </main>
      <Inspector />
    </div>
  )
}

function Toasts() {
  const toasts = useStore((s) => s.toasts)
  return (
    <div className="toasts">
      {toasts.map((t) => (
        <div key={t.id} className={`toast ${t.tone}`}>
          {t.message}
        </div>
      ))}
    </div>
  )
}

function onMenu(action: MenuAction) {
  const s = useStore.getState()
  switch (action) {
    case 'new-session':
      if (s.project) void newSession()
      break
    case 'toggle-inspector':
      if (s.project) toggleInspector()
      break
    case 'toggle-sidebar':
      if (s.project) toggleSidebar()
      break
    case 'settings':
      setState((d) => void (d.settingsOpen = true))
      break
    case 'shortcuts':
      setState((d) => void (d.shortcutsOpen = true))
      break
    case 'stop':
      void interrupt()
      break
    case 'focus-composer':
      focusComposer()
      break
  }
}

export function App() {
  const phase = useStore((s) => s.phase)
  const project = useStore((s) => s.project)
  const theme = useStore((s) => s.settings?.settings.theme ?? 'system')
  const settingsOpen = useStore((s) => s.settingsOpen)
  const shortcutsOpen = useStore((s) => s.shortcutsOpen)
  const rewindTarget = useStore((s) => s.rewindTarget)

  useEffect(() => applyTheme(theme), [theme])
  useEffect(() => host.onMenu(onMenu), [])

  // Keyboard shortcuts that work anywhere in the window.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const s = useStore.getState()
      if (e.key === 'Tab' && e.shiftKey && !e.metaKey && !e.ctrlKey && s.phase === 'ready') {
        e.preventDefault()
        void cycleMode()
        return
      }
      if (e.key === 'Escape' && s.status.busy && !s.permissions.length && !s.plan && !s.settingsOpen && !s.modelPickerOpen && !s.rewindTarget) {
        e.preventDefault()
        void interrupt()
        return
      }
      // In the browser there's no native menu, so handle its shortcuts here.
      if (!isElectron && e.metaKey) {
        const k = e.key.toLowerCase()
        if (k === 'n' && !e.shiftKey) {
          e.preventDefault()
          onMenu('new-session')
        } else if (k === 'i' && e.altKey) {
          e.preventDefault()
          onMenu('toggle-inspector')
        } else if (k === ',') {
          e.preventDefault()
          onMenu('settings')
        } else if (k === '\\') {
          e.preventDefault()
          onMenu('toggle-sidebar')
        } else if (k === '/') {
          e.preventDefault()
          onMenu('shortcuts')
        } else if (k === '.') {
          e.preventDefault()
          onMenu('stop')
        } else if (k === 'l') {
          e.preventDefault()
          onMenu('focus-composer')
        }
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  return (
    <>
      {phase === 'boot' ? null : !project || phase === 'welcome' ? <Welcome /> : <ProjectView />}
      {settingsOpen && <SettingsDialog />}
      {shortcutsOpen && <Shortcuts onClose={() => setState((s) => void (s.shortcutsOpen = false))} />}
      {rewindTarget && <RewindDialog cp={rewindTarget} />}
      <Toasts />
    </>
  )
}
