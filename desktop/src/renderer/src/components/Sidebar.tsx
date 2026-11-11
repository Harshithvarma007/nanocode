import { useState } from 'react'
import { Folder, MessageSquare, PanelLeft, Pencil, Plus, Settings, Trash2 } from 'lucide-react'
import type { SessionInfo } from '@shared/protocol'
import { deleteSession, loadSession, newSession, renameSession, toggleSidebar } from '../lib/actions'
import { basename, plural, relativeTime, tildify } from '../lib/format'
import { setState, useStore } from '../lib/store'
import { Dialog } from './ui'

function SessionRow({ s, active, busy }: { s: SessionInfo; active: boolean; busy: boolean }) {
  const [editing, setEditing] = useState(false)
  const [title, setTitle] = useState(s.title)
  const [confirm, setConfirm] = useState(false)
  const commit = () => {
    setEditing(false)
    if (title.trim() && title.trim() !== s.title) void renameSession(s.id, title)
  }
  return (
    <>
      <div
        className={`sb-item ${active ? 'active' : ''}`}
        onClick={() => !editing && void loadSession(s.id)}
        onDoubleClick={() => {
          setTitle(s.title)
          setEditing(true)
        }}
        title={s.title}
      >
        <div className="sb-item-body">
          {editing ? (
            <input
              className="input sb-rename"
              autoFocus
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              onBlur={commit}
              onClick={(e) => e.stopPropagation()}
              onKeyDown={(e) => {
                if (e.key === 'Enter') commit()
                if (e.key === 'Escape') {
                  e.stopPropagation()
                  setEditing(false)
                }
              }}
            />
          ) : (
            <>
              <div className="sb-item-title">{s.title || 'New session'}</div>
              <div className="sb-item-meta">
                {active && busy ? 'working…' : `${relativeTime(s.updated)} · ${s.turns ? plural(s.turns, 'turn') : 'empty'}`}
              </div>
            </>
          )}
        </div>
        {active && busy && !editing && <span className="busy-dot" title="Working" />}
        {!editing && (
          <div className="sb-item-actions">
            <button
              className="icon-btn sm"
              title="Rename"
              onClick={(e) => {
                e.stopPropagation()
                setTitle(s.title)
                setEditing(true)
              }}
            >
              <Pencil size={12} />
            </button>
            {!active && (
              <button
                className="icon-btn sm"
                title="Delete"
                onClick={(e) => {
                  e.stopPropagation()
                  setConfirm(true)
                }}
              >
                <Trash2 size={12} />
              </button>
            )}
          </div>
        )}
      </div>
      {confirm && (
        <Dialog
          title="Delete this session?"
          icon={<Trash2 size={18} style={{ color: 'var(--err)' }} />}
          onClose={() => setConfirm(false)}
          footer={
            <>
              <button className="btn" onClick={() => setConfirm(false)}>
                Cancel
              </button>
              <button
                className="btn primary"
                style={{ background: 'var(--err)', color: '#fff' }}
                onClick={() => {
                  setConfirm(false)
                  void deleteSession(s.id)
                }}
              >
                Delete
              </button>
            </>
          }
        >
          “{s.title}” and its history will be removed. Files nanocode changed stay as they are.
        </Dialog>
      )}
    </>
  )
}

export function Sidebar() {
  const open = useStore((s) => s.sidebarOpen)
  const project = useStore((s) => s.project)
  const home = useStore((s) => s.ctx?.home ?? '')
  const sessions = useStore((s) => s.sessions)
  const current = useStore((s) => s.session)
  const busy = useStore((s) => s.status.busy)
  const version = useStore((s) => s.version)
  const engineState = useStore((s) => s.engineState)
  const phase = useStore((s) => s.phase)
  const restarting = useStore((s) => s.restarting)
  // The open session is always listed, even before its first turn is saved.
  const list = current && !sessions.some((s) => s.id === current.id) ? [current, ...sessions] : sessions
  const visible = list.filter((s) => s.turns > 0 || s.id === current?.id)

  const dot = restarting ? 'warn' : phase === 'ready' ? 'ok' : phase === 'crashed' || phase === 'not-found' ? 'err' : 'warn'
  const engineText = restarting
    ? 'Restarting engine…'
    : phase === 'ready'
      ? `Engine ${version}${engineState.state === 'running' ? ` · pid ${engineState.pid}` : ''}`
      : phase === 'starting'
        ? 'Starting engine…'
        : 'Engine stopped'

  return (
    <aside className={`sidebar ${open ? '' : 'closed'}`}>
      <div className="sb-top drag">
        <button className="icon-btn" title="Hide sidebar (⌘\)" onClick={toggleSidebar}>
          <PanelLeft size={16} />
        </button>
      </div>
      <div className="sb-project" title={project ?? ''}>
        <div className="sb-project-mark">
          <Folder size={16} />
        </div>
        <div style={{ minWidth: 0 }}>
          <div className="sb-project-name">{project ? basename(project) : 'nanocode'}</div>
          <div className="sb-project-path"><bdi>{project ? tildify(project, home) : ''}</bdi></div>
        </div>
      </div>
      <button className="sb-new" onClick={() => void newSession()} disabled={phase !== 'ready'}>
        <Plus size={15} />
        New session
        <span className="kbd">⌘N</span>
      </button>
      <div className="sb-label">Sessions</div>
      <div className="sb-list">
        {visible.length === 0 && (
          <div className="sb-empty">
            <MessageSquare size={13} style={{ verticalAlign: -2, marginRight: 6 }} />
            No sessions yet
          </div>
        )}
        {visible.map((s) => (
          <SessionRow key={s.id} s={s} active={s.id === current?.id} busy={busy} />
        ))}
      </div>
      <div className="sb-bottom">
        <div className="sb-engine" title={'command' in engineState ? engineState.command : ''}>
          <span className={`dot ${dot}`} />
          {engineText}
        </div>
        <button className="icon-btn" title="Settings (⌘,)" onClick={() => setState((s) => void (s.settingsOpen = true))}>
          <Settings size={16} />
        </button>
      </div>
    </aside>
  )
}
