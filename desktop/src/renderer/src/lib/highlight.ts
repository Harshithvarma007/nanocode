// Syntax highlighting with highlight.js core and a handful of common languages (keeps the bundle small).
import hljs from 'highlight.js/lib/core'
import bash from 'highlight.js/lib/languages/bash'
import c from 'highlight.js/lib/languages/c'
import cpp from 'highlight.js/lib/languages/cpp'
import css from 'highlight.js/lib/languages/css'
import diff from 'highlight.js/lib/languages/diff'
import go from 'highlight.js/lib/languages/go'
import ini from 'highlight.js/lib/languages/ini'
import java from 'highlight.js/lib/languages/java'
import javascript from 'highlight.js/lib/languages/javascript'
import json from 'highlight.js/lib/languages/json'
import markdown from 'highlight.js/lib/languages/markdown'
import python from 'highlight.js/lib/languages/python'
import ruby from 'highlight.js/lib/languages/ruby'
import rust from 'highlight.js/lib/languages/rust'
import shell from 'highlight.js/lib/languages/shell'
import sql from 'highlight.js/lib/languages/sql'
import swift from 'highlight.js/lib/languages/swift'
import typescript from 'highlight.js/lib/languages/typescript'
import xml from 'highlight.js/lib/languages/xml'
import yaml from 'highlight.js/lib/languages/yaml'

const languages = { bash, c, cpp, css, diff, go, ini, java, javascript, json, markdown, python, ruby, rust, shell, sql, swift, typescript, xml, yaml }
for (const [name, lang] of Object.entries(languages)) hljs.registerLanguage(name, lang)
hljs.registerAliases(['sh', 'zsh', 'console'], { languageName: 'bash' })
hljs.registerAliases(['js', 'jsx', 'mjs', 'cjs'], { languageName: 'javascript' })
hljs.registerAliases(['ts', 'tsx'], { languageName: 'typescript' })
hljs.registerAliases(['py'], { languageName: 'python' })
hljs.registerAliases(['html', 'svg'], { languageName: 'xml' })
hljs.registerAliases(['toml'], { languageName: 'ini' })
hljs.registerAliases(['yml'], { languageName: 'yaml' })
hljs.registerAliases(['md'], { languageName: 'markdown' })
hljs.registerAliases(['rs'], { languageName: 'rust' })

const EXT: Record<string, string> = { py: 'python', js: 'javascript', mjs: 'javascript', cjs: 'javascript', jsx: 'javascript', ts: 'typescript', tsx: 'typescript',
  json: 'json', css: 'css', html: 'xml', xml: 'xml', svg: 'xml', md: 'markdown', yml: 'yaml', yaml: 'yaml', toml: 'ini', ini: 'ini', sh: 'bash', zsh: 'bash',
  rs: 'rust', go: 'go', java: 'java', c: 'c', h: 'c', cpp: 'cpp', cc: 'cpp', hpp: 'cpp', rb: 'ruby', swift: 'swift', sql: 'sql' }

export function langForPath(path: string): string | undefined {
  const ext = path.split('.').pop()?.toLowerCase() ?? ''
  return EXT[ext]
}

/** Highlighted HTML, or null when the language is unknown. */
export function highlight(code: string, lang?: string): string | null {
  if (!lang) return null
  const name = lang.toLowerCase()
  if (!hljs.getLanguage(name)) return null
  try {
    return hljs.highlight(code, { language: name, ignoreIllegals: true }).value
  } catch {
    return null
  }
}

/** Highlight line by line (for the diff viewer); state across lines is not kept, which is fine for diffs. */
export function highlightLine(line: string, lang?: string): string | null {
  return highlight(line, lang)
}
