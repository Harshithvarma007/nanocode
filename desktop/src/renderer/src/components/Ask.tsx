// Inline approval cards: permission.request and plan.request. Keyboard-first like the TUI:
// Enter / 1 = yes, 2 = the "always" option, Esc / 3 = no (and then type what to do instead).
import { useEffect, useRef, useState } from 'react'
import { ClipboardList, ShieldCheck } from 'lucide-react'
import { respondPermission, respondPlan } from '../lib/actions'
import type { PermissionRequest, PlanRequest } from '../lib/store'
import { DiffView } from './DiffView'
import { Markdown } from './Markdown'

function inlineCode(text: string) {
  return text.split(/(`[^`]+`)/g).map((part, i) => (part.startsWith('`') && part.endsWith('`') ? <code key={i}>{part.slice(1, -1)}</code> : part))
}

function isTyping(): boolean {
  const el = document.activeElement
  return Boolean(el && (el.tagName === 'TEXTAREA' || el.tagName === 'INPUT') && !(el as HTMLElement).dataset.askFeedback)
}

interface Option {
  key: string
  label: string
  hint?: string
}

function useAskKeys(active: boolean, options: Option[], onPick: (index: number) => void, feedbackOpen: boolean) {
  const [selected, setSelected] = useState(0)
  useEffect(() => {
    if (!active || feedbackOpen) return
    const onKey = (e: KeyboardEvent) => {
      if (e.metaKey || e.ctrlKey || e.altKey) return
      const composerEmpty = !(document.querySelector<HTMLTextAreaElement>('[data-composer]')?.value ?? '')
      if (isTyping() && !(composerEmpty && ['Enter', 'Escape', '1', '2', '3', 'ArrowUp', 'ArrowDown'].includes(e.key))) return
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault()
        setSelected((s) => (s + (e.key === 'ArrowDown' ? 1 : options.length - 1)) % options.length)
      } else if (e.key === 'Enter') {
        e.preventDefault()
        e.stopPropagation()
        onPick(selected)
      } else if (e.key === 'Escape') {
        e.preventDefault()
        e.stopPropagation()
        onPick(options.length - 1)
      } else if (/^[1-9]$/.test(e.key) && Number(e.key) <= options.length) {
        e.preventDefault()
        onPick(Number(e.key) - 1)
      }
    }
    window.addEventListener('keydown', onKey, true)
    return () => window.removeEventListener('keydown', onKey, true)
  }, [active, options.length, onPick, selected, feedbackOpen])
  return [selected, setSelected] as const
}

function Feedback({ placeholder, onSubmit }: { placeholder: string; onSubmit: (text: string) => void }) {
  const [text, setText] = useState('')
  const ref = useRef<HTMLInputElement>(null)
  useEffect(() => ref.current?.focus(), [])
  return (
    <div className="ask-feedback">
      <input
        ref={ref}
        data-ask-feedback="1"
        className="input"
        placeholder={placeholder}
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter') {
            e.preventDefault()
            onSubmit(text.trim())
          } else if (e.key === 'Escape') {
            e.preventDefault()
            e.stopPropagation()
            onSubmit('')
          }
        }}
      />
      <button className="btn primary" onClick={() => onSubmit(text.trim())}>
        Send
      </button>
    </div>
  )
}

function OptionList({ options, selected, onHover, onPick }: { options: Option[]; selected: number; onHover: (i: number) => void; onPick: (i: number) => void }) {
  return (
    <div className="ask-options" role="listbox">
      {options.map((o, i) => (
        <button key={o.key} role="option" aria-selected={i === selected} className={`ask-option ${i === selected ? 'selected' : ''}`} onMouseEnter={() => onHover(i)} onClick={() => onPick(i)}>
          <span className="kbd">{i + 1}</span>
          <span>{inlineCode(o.label)}</span>
          {o.hint && <span className="ask-option-hint">{o.hint}</span>}
        </button>
      ))}
    </div>
  )
}

export function PermissionCard({ req, active }: { req: PermissionRequest; active: boolean }) {
  const [feedback, setFeedback] = useState(false)
  const cardRef = useRef<HTMLDivElement>(null)
  const options: Option[] = [
    { key: 'yes', label: 'Yes', hint: 'enter' },
    { key: 'always', label: req.alwaysLabel || "Yes, and don't ask again this session" },
    { key: 'no', label: 'No, and tell nanocode what to do differently', hint: 'esc' }
  ]
  const pick = (i: number) => {
    if (i === 0) void respondPermission(req.requestId, 'yes')
    else if (i === 1) void respondPermission(req.requestId, 'always')
    else setFeedback(true)
  }
  const [selected, setSelected] = useAskKeys(active, options, pick, feedback)
  useEffect(() => {
    if (active) cardRef.current?.scrollIntoView({ block: 'nearest' })
  }, [active])

  return (
    <div ref={cardRef} className="ask" tabIndex={-1}>
      <div className="ask-head">
        <ShieldCheck size={15} />
        {req.title}
      </div>
      <div className="ask-question">{req.question}</div>
      {req.preview && (
        <div className={`ask-preview ${req.previewKind}`}>
          {req.previewKind === 'diff' ? (
            <DiffView diff={req.preview} maxRows={80} />
          ) : req.previewKind === 'command' ? (
            <pre className="selectable">{req.preview}</pre>
          ) : req.previewKind === 'url' ? (
            <div className="selectable">{req.preview}</div>
          ) : (
            <pre className="output selectable">{req.preview}</pre>
          )}
        </div>
      )}
      {feedback ? (
        <Feedback placeholder="What should nanocode do instead? (Enter to send, Esc to just say no)" onSubmit={(text) => void respondPermission(req.requestId, 'no', text)} />
      ) : (
        <OptionList options={options} selected={selected} onHover={setSelected} onPick={pick} />
      )}
    </div>
  )
}

export function PlanCard({ req }: { req: PlanRequest }) {
  const [feedback, setFeedback] = useState(false)
  const cardRef = useRef<HTMLDivElement>(null)
  const options: Option[] = [
    { key: 'accept-edits', label: 'Yes, and auto-accept edits', hint: 'enter' },
    { key: 'default', label: 'Yes, and manually approve edits' },
    { key: 'no', label: 'No, keep planning', hint: 'esc' }
  ]
  const pick = (i: number) => {
    if (i === 0) void respondPlan(req.requestId, 'accept-edits')
    else if (i === 1) void respondPlan(req.requestId, 'default')
    else setFeedback(true)
  }
  const [selected, setSelected] = useAskKeys(true, options, pick, feedback)
  useEffect(() => {
    cardRef.current?.scrollIntoView({ block: 'nearest' })
  }, [])
  return (
    <div ref={cardRef} className="ask plan" tabIndex={-1}>
      <div className="ask-head">
        <ClipboardList size={15} />
        Ready to code?
      </div>
      <div className="ask-question">Here is nanocode's plan. Would you like to proceed?</div>
      <div className="ask-plan">
        <Markdown text={req.plan} />
      </div>
      {feedback ? (
        <Feedback placeholder="What should change in the plan? (Enter to send, Esc to just stop)" onSubmit={(text) => void respondPlan(req.requestId, 'no', text)} />
      ) : (
        <OptionList options={options} selected={selected} onHover={setSelected} onPick={pick} />
      )}
    </div>
  )
}
