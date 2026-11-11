// Small shared pieces: the nanocode mark, the TUI-style spinner glyph, tooltips, dialogs.
import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'

/** The ✻ mark from the terminal UI, drawn as an SVG so it stays crisp at any size. */
export function Mark({ size = 20, strokeWidth = 2.4 }: { size?: number; strokeWidth?: number }) {
  const arms = [0, 45, 90, 135]
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" aria-hidden>
      {arms.map((a) => (
        <line key={a} x1="12" y1="3.2" x2="12" y2="20.8" stroke="currentColor" strokeWidth={strokeWidth} strokeLinecap="round" transform={`rotate(${a} 12 12)`} />
      ))}
    </svg>
  )
}

const FRAMES = ['·', '✢', '✳', '✶', '✻', '✽', '✻', '✶', '✳', '✢']

/** The terminal UI's spinner: ·✢✳✶✻✽ cycling at 8 fps. */
export function Glyph({ active = true }: { active?: boolean }) {
  const [i, setI] = useState(4)
  useEffect(() => {
    if (!active) return
    const t = setInterval(() => setI((n) => (n + 1) % FRAMES.length), 125)
    return () => clearInterval(t)
  }, [active])
  return <span className="glyph">{active ? FRAMES[i] : '✻'}</span>
}

export function Tooltip({ children, content, delay = 350, disabled = false }: { children: ReactNode; content: ReactNode; delay?: number; disabled?: boolean }) {
  const ref = useRef<HTMLSpanElement>(null)
  const tip = useRef<HTMLDivElement>(null)
  const [open, setOpen] = useState(false)
  const [pos, setPos] = useState<{ left: number; top: number } | null>(null)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)
  useLayoutEffect(() => {
    if (!open || !ref.current || !tip.current) return
    const r = ref.current.getBoundingClientRect()
    const t = tip.current.getBoundingClientRect()
    let left = r.left + r.width / 2 - t.width / 2
    left = Math.max(8, Math.min(left, window.innerWidth - t.width - 8))
    let top = r.top - t.height - 8
    if (top < 8) top = r.bottom + 8
    setPos({ left, top })
  }, [open, content])
  return (
    <span
      ref={ref}
      style={{ display: 'inline-flex' }}
      onMouseEnter={() => (timer.current = setTimeout(() => setOpen(true), delay))}
      onMouseLeave={() => {
        if (timer.current) clearTimeout(timer.current)
        setOpen(false)
        setPos(null)
      }}
    >
      {children}
      {open &&
        !disabled &&
        createPortal(
          <div ref={tip} className="tooltip" style={pos ? { left: pos.left, top: pos.top } : { left: -9999, top: -9999 }}>
            {content}
          </div>,
          document.body
        )}
    </span>
  )
}

export function Dialog({
  title,
  icon,
  children,
  footer,
  onClose,
  className = ''
}: {
  title: ReactNode
  icon?: ReactNode
  children: ReactNode
  footer?: ReactNode
  onClose: () => void
  className?: string
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.stopPropagation()
        onClose()
      }
    }
    window.addEventListener('keydown', onKey, true)
    return () => window.removeEventListener('keydown', onKey, true)
  }, [onClose])
  return createPortal(
    <div className="overlay" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className={`dialog ${className}`} role="dialog" aria-modal="true">
        <div className="dialog-head">
          {icon}
          <h2>{title}</h2>
        </div>
        <div className="dialog-body">{children}</div>
        {footer && <div className="dialog-foot">{footer}</div>}
      </div>
    </div>,
    document.body
  )
}

/** Close a popover when clicking outside of it. */
export function useClickOutside(ref: React.RefObject<HTMLElement | null>, onOutside: () => void, active = true) {
  useEffect(() => {
    if (!active) return
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) onOutside()
    }
    window.addEventListener('mousedown', onDown)
    return () => window.removeEventListener('mousedown', onDown)
  }, [ref, onOutside, active])
}
