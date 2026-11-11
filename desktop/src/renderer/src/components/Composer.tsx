import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { ArrowUp, Check, ChevronDown, Cpu, FileText, Hash, KeyRound, Square, SquareTerminal } from 'lucide-react'
import type { ModelOption } from '@shared/protocol'
import { cycleMode, interrupt, listModels, openInspector, refreshContext, searchFiles, sendPrompt, setModel } from '../lib/actions'
import { fmtTokens, MODES, modelName } from '../lib/format'
import { host } from '../lib/host'
import { setState, useStore } from '../lib/store'
import { Tooltip, useClickOutside } from './ui'

type Popup =
  | { kind: 'commands'; items: { name: string; description: string }[] }
  | { kind: 'files'; items: string[]; start: number; end: number; query: string }
  | null

function setDraft(text: string) {
  setState((s) => void (s.draft = text))
}

// ------------------------------------------------------------------ mode pill

function ModePill() {
  const mode = useStore((s) => s.status.mode)
  const ready = useStore((s) => s.phase === 'ready')
  const info = MODES[mode] ?? MODES.default
  return (
    <Tooltip
      content={
        <>
          <b>{info.label}</b> · {info.hint}
          <br />
          Click or press ⇧⇥ to cycle: default → accept edits → plan
        </>
      }
    >
      <button className={`pill mode ${mode}`} onClick={() => void cycleMode()} disabled={!ready}>
        <span className="mode-dot" />
        {info.short}
      </button>
    </Tooltip>
  )
}

// ------------------------------------------------------------------ model picker

function ModelPicker() {
  const open = useStore((s) => s.modelPickerOpen)
  const current = useStore((s) => s.status.model)
  const busy = useStore((s) => s.status.busy)
  const ready = useStore((s) => s.phase === 'ready')
  const [options, setOptions] = useState<ModelOption[] | null>(null)
  const [query, setQuery] = useState('')
  const [custom, setCustom] = useState('')
  const [error, setError] = useState('')
  const [pending, setPending] = useState('')
  const [sel, setSel] = useState(0)
  const ref = useRef<HTMLDivElement>(null)
  const close = useCallback(() => setState((s) => void (s.modelPickerOpen = false)), [])
  useClickOutside(ref, close, open)

  useEffect(() => {
    if (!open) return
    setQuery('')
    setError('')
    setSel(0)
    setOptions(null)
    listModels()
      .then((r) => setOptions(r.options))
      .catch((e) => setError(String(e.message ?? e)))
  }, [open])

  const filtered = useMemo(() => {
    const q = query.toLowerCase()
    return (options ?? []).filter((o) => !q || `${o.spec} ${o.label} ${o.detail} ${o.group}`.toLowerCase().includes(q))
  }, [options, query])
  const groups = useMemo(() => {
    const map = new Map<string, ModelOption[]>()
    for (const o of filtered) map.set(o.group, [...(map.get(o.group) ?? []), o])
    // Groups you can use right now come first.
    return [...map.entries()].sort((a, b) => Number(b[1].some((o) => o.available)) - Number(a[1].some((o) => o.available)))
  }, [filtered])

  const choose = async (spec: string) => {
    setPending(spec)
    setError('')
    try {
      await setModel(spec)
      close()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setPending('')
    }
  }

  const selectable = filtered.filter((o) => o.available)
  return (
    <div ref={ref} style={{ position: 'relative' }}>
      <Tooltip content={busy ? 'You can switch models when nanocode is idle' : 'Switch model'} disabled={open}>
        <button className="pill" disabled={!ready || busy} onClick={() => setState((s) => void (s.modelPickerOpen = !s.modelPickerOpen))}>
          <Cpu size={13} />
          {modelName(current)}
          <ChevronDown size={12} style={{ opacity: 0.6 }} />
        </button>
      </Tooltip>
      {open && (
        <div className="popover above models">
          <div className="models-search">
            <input
              className="input"
              autoFocus
              placeholder="Search models…"
              value={query}
              onChange={(e) => {
                setQuery(e.target.value)
                setSel(0)
              }}
              onKeyDown={(e) => {
                if (e.key === 'Escape') {
                  e.stopPropagation()
                  close()
                } else if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
                  e.preventDefault()
                  const n = selectable.length || 1
                  setSel((v) => (v + (e.key === 'ArrowDown' ? 1 : n - 1)) % n)
                } else if (e.key === 'Enter' && selectable[sel]) {
                  e.preventDefault()
                  void choose(selectable[sel].spec)
                }
              }}
            />
          </div>
          <div className="models-list">
            {!options && !error && <div className="palette-empty">Looking for models…</div>}
            {options && !filtered.length && <div className="palette-empty">No models match. Type a provider:model below.</div>}
            {groups.map(([group, list]) => (
              <div key={group}>
                <div className="models-group">{group}</div>
                {list.map((o) => {
                  const isCurrent = o.spec === current
                  const isSel = selectable[sel]?.spec === o.spec
                  return (
                    <button
                      key={o.spec}
                      ref={isCurrent ? (el) => el?.scrollIntoView({ block: 'nearest' }) : undefined}
                      className={`model-opt ${isSel ? 'selected' : ''}`}
                      disabled={!o.available || pending !== ''}
                      onClick={() => void choose(o.spec)}
                      title={o.spec}
                    >
                      <span className="model-check">{isCurrent && <Check size={14} />}</span>
                      <span className="model-opt-body">
                        <div className="model-opt-label">{pending === o.spec ? `Connecting to ${o.label}…` : o.label}</div>
                        <div className="model-opt-detail">{o.detail || o.spec}</div>
                        {!o.available && o.needs && (
                          <div className="model-opt-needs">
                            <KeyRound size={11} style={{ verticalAlign: -1, marginRight: 4 }} />
                            Needs {o.needs}
                          </div>
                        )}
                      </span>
                      {!o.available && o.needs && (
                        <span
                          className="link-btn"
                          role="button"
                          onClick={(e) => {
                            e.stopPropagation()
                            close()
                            setState((s) => void (s.settingsOpen = true))
                          }}
                          style={{ pointerEvents: 'auto', whiteSpace: 'nowrap' }}
                        >
                          Add key
                        </span>
                      )}
                    </button>
                  )
                })}
              </div>
            ))}
          </div>
          {error && <div className="models-error selectable">{error}</div>}
          <div className="models-foot">
            <input
              className="input mono"
              placeholder="Any provider:model, e.g. openrouter:qwen/qwen3-coder"
              value={custom}
              onChange={(e) => setCustom(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && custom.trim()) void choose(custom.trim())
                if (e.key === 'Escape') {
                  e.stopPropagation()
                  close()
                }
              }}
            />
            <button className="btn" disabled={!custom.trim() || pending !== ''} onClick={() => void choose(custom.trim())}>
              Use
            </button>
          </div>
        </div>
      )}
    </div>
  )
}

// ------------------------------------------------------------------ context ring

function ContextRing() {
  const used = useStore((s) => s.status.context_used)
  const limit = useStore((s) => s.status.context_limit)
  const ctx = useStore((s) => s.context)
  const pct = limit ? Math.min(100, (100 * used) / limit) : 0
  const r = 7
  const c = 2 * Math.PI * r
  const color = pct >= (ctx?.compact_at ?? 0.85) * 100 ? 'var(--err)' : pct >= (ctx?.microcompact_at ?? 0.6) * 100 ? 'var(--warn)' : 'var(--accent)'
  return (
    <Tooltip
      content={
        <>
          <b>Context: {Math.round(pct)}% used</b>
          <br />
          {fmtTokens(used)} of {fmtTokens(limit)} tokens
          {ctx && (
            <>
              <br />
              System {fmtTokens(ctx.system)} · Tools {fmtTokens(ctx.tools)} · Messages {fmtTokens(ctx.messages)}
              <br />
              Old tool results are cleared at {Math.round(ctx.microcompact_at * 100)}%, and the conversation is compacted at{' '}
              {Math.round(ctx.compact_at * 100)}%.
            </>
          )}
        </>
      }
    >
      <button className="ring" onMouseEnter={() => void refreshContext()} onClick={() => openInspector('context')}>
        <svg width="18" height="18" viewBox="0 0 18 18">
          <circle cx="9" cy="9" r={r} fill="none" stroke="var(--surface-3)" strokeWidth="2.2" />
          <circle
            cx="9"
            cy="9"
            r={r}
            fill="none"
            stroke={color}
            strokeWidth="2.2"
            strokeLinecap="round"
            strokeDasharray={c}
            strokeDashoffset={c * (1 - pct / 100)}
            transform="rotate(-90 9 9)"
          />
        </svg>
        {Math.round(pct)}%
      </button>
    </Tooltip>
  )
}

// ------------------------------------------------------------------ the composer

export function Composer() {
  const draft = useStore((s) => s.draft)
  const busy = useStore((s) => s.status.busy)
  const ready = useStore((s) => s.phase === 'ready')
  const commands = useStore((s) => s.commands)
  const project = useStore((s) => s.project)
  const asking = useStore((s) => s.permissions.length > 0 || s.plan !== null)
  const ref = useRef<HTMLTextAreaElement>(null)
  const [popup, setPopup] = useState<Popup>(null)
  const [sel, setSel] = useState(0)
  const [dragging, setDragging] = useState(false)
  const searchSeq = useRef(0)

  const fit = useCallback(() => {
    const el = ref.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, 260)}px`
  }, [])
  useLayoutEffect(fit, [draft, ready, busy, fit])
  useEffect(() => {
    const el = ref.current
    if (!el) return
    let width = el.clientWidth
    const ro = new ResizeObserver(() => {
      if (el.clientWidth !== width) {
        width = el.clientWidth
        fit()
      }
    })
    ro.observe(el)
    return () => ro.disconnect()
  }, [fit])

  useEffect(() => {
    if (ready) ref.current?.focus()
  }, [ready])

  const updatePopup = (value: string, cursor: number) => {
    const before = value.slice(0, cursor)
    if (value.startsWith('/') && !/\s/.test(before)) {
      const q = before.slice(1).toLowerCase()
      const items = commands.filter((c) => c.name.toLowerCase().startsWith(q) || (q.length > 1 && c.name.toLowerCase().includes(q)))
      setPopup(items.length ? { kind: 'commands', items } : null)
      setSel(0)
      return
    }
    const m = /(^|\s)@([^\s@]*)$/.exec(before)
    if (m) {
      const query = m[2]
      const start = cursor - query.length - 1
      const seq = ++searchSeq.current
      void searchFiles(query).then((files) => {
        if (seq !== searchSeq.current) return
        setPopup({ kind: 'files', items: files.slice(0, 12), start, end: cursor, query })
        setSel(0)
      })
      return
    }
    searchSeq.current++
    setPopup(null)
  }

  const insertAt = (start: number, end: number, text: string) => {
    const value = useStore.getState().draft
    const next = value.slice(0, start) + text + value.slice(end)
    setDraft(next)
    requestAnimationFrame(() => {
      const el = ref.current
      if (!el) return
      el.focus()
      el.selectionStart = el.selectionEnd = start + text.length
    })
  }

  const accept = (index: number, submit: boolean) => {
    if (!popup) return
    if (popup.kind === 'commands') {
      const cmd = popup.items[index]
      if (!cmd) return
      setPopup(null)
      if (submit) {
        setDraft('')
        void sendPrompt('/' + cmd.name).then((ok) => !ok && setDraft('/' + cmd.name))
      } else {
        setDraft(`/${cmd.name} `)
        requestAnimationFrame(() => ref.current?.focus())
      }
    } else {
      const file = popup.items[index]
      if (!file) return
      setPopup(null)
      insertAt(popup.start, popup.end, `@${file} `)
    }
  }

  const submit = async () => {
    const text = useStore.getState().draft
    if (!text.trim() || busy) return
    setDraft('')
    setPopup(null)
    const ok = await sendPrompt(text)
    if (!ok && !useStore.getState().draft) setDraft(text)
  }

  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.nativeEvent.isComposing) return
    if (popup && popup.items.length) {
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault()
        const n = popup.items.length
        setSel((v) => (v + (e.key === 'ArrowDown' ? 1 : n - 1)) % n)
        return
      }
      if (e.key === 'Tab' && !e.shiftKey) {
        e.preventDefault()
        accept(sel, false)
        return
      }
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault()
        accept(sel, popup.kind === 'commands')
        return
      }
      if (e.key === 'Escape') {
        e.preventDefault()
        e.stopPropagation()
        setPopup(null)
        return
      }
    }
    if (e.key === 'Enter' && !e.shiftKey && !e.altKey) {
      e.preventDefault()
      void submit()
    }
  }

  const relative = (p: string) => (project && p.startsWith(project + '/') ? p.slice(project.length + 1) : p)

  const onDrop = async (e: React.DragEvent) => {
    e.preventDefault()
    setDragging(false)
    const files = [...e.dataTransfer.files]
    if (!files.length) return
    const mentions: string[] = []
    for (const f of files) {
      let p = ''
      try {
        p = host.pathForFile(f)
      } catch {
        p = ''
      }
      if (p) mentions.push('@' + relative(p))
      else {
        const hits = await searchFiles(f.name)
        mentions.push('@' + (hits.find((h) => h.endsWith('/' + f.name) || h === f.name) ?? f.name))
      }
    }
    const el = ref.current
    const value = useStore.getState().draft
    const at = el ? el.selectionStart : value.length
    const pad = at > 0 && !/\s$/.test(value.slice(0, at)) ? ' ' : ''
    insertAt(at, at, pad + mentions.join(' ') + ' ')
  }

  const prefix = draft.startsWith('!') ? 'shell' : draft.startsWith('#') && !draft.startsWith('##') ? 'memory' : ''
  const placeholder = !ready
    ? 'Starting nanocode…'
    : busy
      ? asking
        ? 'nanocode is waiting for your answer above'
        : 'nanocode is working… press Esc to stop'
      : 'Ask nanocode to build, fix or explain anything'

  return (
    <div className="composer-wrap">
      <div
        className={`composer ${prefix} ${dragging ? 'dragging' : ''}`}
        onDragOver={(e) => {
          if (e.dataTransfer.types.includes('Files')) {
            e.preventDefault()
            setDragging(true)
          }
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => void onDrop(e)}
      >
        {popup?.kind === 'commands' && (
          <div className="popover above palette" role="listbox">
            <div className="palette-title">Commands</div>
            {popup.items.map((c, i) => (
              <button key={c.name} className={`palette-item ${i === sel ? 'selected' : ''}`} onMouseEnter={() => setSel(i)} onMouseDown={(e) => {
                e.preventDefault()
                accept(i, true)
              }}>
                <span className="palette-item-name">/{c.name}</span>
                <span className="palette-item-desc">{c.description}</span>
              </button>
            ))}
          </div>
        )}
        {popup?.kind === 'files' && (
          <div className="popover above palette" role="listbox">
            <div className="palette-title">Files</div>
            {!popup.items.length && <div className="palette-empty">No files match “{popup.query}”</div>}
            {popup.items.map((f, i) => {
              const slash = f.lastIndexOf('/')
              return (
                <button key={f} className={`palette-item ${i === sel ? 'selected' : ''}`} onMouseEnter={() => setSel(i)} onMouseDown={(e) => {
                  e.preventDefault()
                  accept(i, false)
                }}>
                  <span className="palette-file">
                    <FileText size={13} style={{ color: 'var(--text-3)' }} />
                    <span>
                      {slash >= 0 && <span>{f.slice(0, slash + 1)}</span>}
                      <b>{f.slice(slash + 1)}</b>
                    </span>
                  </span>
                </button>
              )
            })}
          </div>
        )}
        {prefix === 'shell' && (
          <div className="composer-hint">
            <SquareTerminal size={13} /> <b>!</b> Run a shell command yourself. The output is shared with nanocode.
          </div>
        )}
        {prefix === 'memory' && (
          <div className="composer-hint">
            <Hash size={13} /> <b>#</b> Save a note to AGENTS.md. It's loaded in every future session.
          </div>
        )}
        <textarea
          ref={ref}
          data-composer
          rows={1}
          value={draft}
          placeholder={placeholder}
          disabled={!ready}
          spellCheck
          onChange={(e) => {
            setDraft(e.target.value)
            updatePopup(e.target.value, e.target.selectionStart)
          }}
          onKeyDown={onKeyDown}
          onBlur={() => setTimeout(() => setPopup(null), 120)}
        />
        <div className="composer-bar">
          <ModePill />
          <ModelPicker />
          <div className="spacer" />
          <ContextRing />
          {busy ? (
            <Tooltip content="Stop (Esc)">
              <button className="send-btn stop" onClick={() => void interrupt()} aria-label="Stop">
                <Square size={12} fill="currentColor" />
              </button>
            </Tooltip>
          ) : (
            <button className="send-btn" disabled={!ready || !draft.trim()} onClick={() => void submit()} aria-label="Send">
              <ArrowUp size={17} strokeWidth={2.4} />
            </button>
          )}
        </div>
      </div>
    </div>
  )
}
