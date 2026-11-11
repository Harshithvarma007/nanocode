import { memo, useState, type ReactNode } from 'react'
import ReactMarkdown, { type Components } from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { Check, Copy } from 'lucide-react'
import { highlight } from '../lib/highlight'

function textOf(node: ReactNode): string {
  if (typeof node === 'string' || typeof node === 'number') return String(node)
  if (Array.isArray(node)) return node.map(textOf).join('')
  if (node && typeof node === 'object' && 'props' in node) return textOf((node as { props: { children?: ReactNode } }).props.children)
  return ''
}

export function CopyButton({ text, label = 'Copy' }: { text: string; label?: string }) {
  const [copied, setCopied] = useState(false)
  return (
    <button
      className="copy-btn"
      title="Copy to clipboard"
      onClick={() => {
        void navigator.clipboard.writeText(text)
        setCopied(true)
        setTimeout(() => setCopied(false), 1400)
      }}
    >
      {copied ? <Check size={12} /> : <Copy size={12} />}
      {copied ? 'Copied' : label}
    </button>
  )
}

export function CodeBlock({ code, lang }: { code: string; lang?: string }) {
  const html = highlight(code, lang)
  return (
    <div className="codeblock">
      <div className="codeblock-head">
        <span>{lang || 'text'}</span>
        <CopyButton text={code} />
      </div>
      <pre className="selectable">
        {html ? <code dangerouslySetInnerHTML={{ __html: html }} /> : <code>{code}</code>}
      </pre>
    </div>
  )
}

const components: Components = {
  pre({ children }) {
    const child = Array.isArray(children) ? children[0] : children
    const props = (child as { props?: { className?: string; children?: ReactNode } })?.props ?? {}
    const lang = /language-([\w+#-]+)/.exec(props.className ?? '')?.[1]
    const code = textOf(props.children).replace(/\n$/, '')
    return <CodeBlock code={code} lang={lang} />
  },
  a({ href, children }) {
    return (
      <a href={href} target="_blank" rel="noreferrer">
        {children}
      </a>
    )
  }
}

const plugins = [remarkGfm]

export const Markdown = memo(function Markdown({ text, streaming = false, className = '' }: { text: string; streaming?: boolean; className?: string }) {
  return (
    <div className={`md selectable ${streaming ? 'streaming' : ''} ${className}`}>
      <ReactMarkdown remarkPlugins={plugins} components={components}>
        {text}
      </ReactMarkdown>
    </div>
  )
})
