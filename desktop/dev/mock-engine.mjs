#!/usr/bin/env node
// A mock nanocode engine for UI development. It speaks docs/PROTOCOL.md over stdio and replays
// realistic turns: streaming thinking + text, tool calls with diffs, permission and plan requests,
// todos, a subagent Task with nested events, auto-compaction, errors and interrupts.
// It never writes to disk: edits and checkpoints are simulated.
//
//   node dev/mock-engine.mjs serve --cwd <dir> [--model provider:model]
//   MOCK_SPEED=3 makes everything three times faster.
//
// Prompts it understands (anything else runs the "fix the failing tests" turn):
//   "explain …"  · "plan …" (or plan mode) · "error" · "long" (a slow turn to test Stop)
//   /help /compact /clear /context /cost /init /model /mode /effort /think /todos /memory /status
//   !<command>   #<note>

import fs from 'node:fs'
import path from 'node:path'
import readline from 'node:readline'

const argv = process.argv.slice(2)
const flag = (name) => {
  const i = argv.indexOf(name)
  return i >= 0 ? argv[i + 1] : undefined
}
const CWD = path.resolve(flag('--cwd') ?? process.cwd())
const SPEED = Number(process.env.MOCK_SPEED || 1)
const VERSION = '0.3.0-mock'

const out = (obj) => process.stdout.write(JSON.stringify(obj) + '\n')
const logErr = (...a) => process.stderr.write(`[mock] ${a.join(' ')}\n`)
const now = () => Math.floor(Date.now() / 1000)

// ------------------------------------------------------------------ models

const MODELS = [
  { spec: 'anthropic:claude-opus-5-5', label: 'Claude Opus 5.5', detail: 'most capable · $4 / $20 per MTok', group: 'Anthropic', key: 'ANTHROPIC_API_KEY', window: 200000 },
  { spec: 'anthropic:claude-sonnet-5-5', label: 'Claude Sonnet 5.5', detail: 'fast everyday coding · $2 / $10', group: 'Anthropic', key: 'ANTHROPIC_API_KEY', window: 200000 },
  { spec: 'anthropic:claude-haiku-5-5', label: 'Claude Haiku 5.5', detail: 'cheapest and fastest · $0.10 / $0.50', group: 'Anthropic', key: 'ANTHROPIC_API_KEY', window: 200000 },
  { spec: 'openai:gpt-5', label: 'GPT-5', detail: 'or type openai:<any model>', group: 'OpenAI-compatible', key: 'OPENAI_API_KEY', window: 272000 },
  { spec: 'openrouter:qwen/qwen3-coder', label: 'Qwen3 Coder (OpenRouter)', detail: 'via OpenRouter', group: 'OpenAI-compatible', key: 'OPENROUTER_API_KEY', window: 262000 },
  { spec: 'ollama:qwen3:8b', label: 'qwen3:8b', detail: 'local · 8.2B · tools, thinking · free', group: 'Local (Ollama)', window: 32768 },
  { spec: 'ollama:llama3.1:8b', label: 'llama3.1:8b', detail: 'local · 8.0B · tools · free', group: 'Local (Ollama)', window: 32768 },
  { spec: 'ollama:qwen3.5:4b', label: 'qwen3.5:4b', detail: 'local · 4.7B · tools, thinking · free', group: 'Local (Ollama)', window: 32768 }
]
const KEYS = ['ANTHROPIC_API_KEY', 'OPENAI_API_KEY', 'OPENROUTER_API_KEY', 'GEMINI_API_KEY', 'GROQ_API_KEY']
const hasKey = (env) => Boolean(process.env[env])
const available = (m) => !m.key || hasKey(m.key)
const pickDefault = () =>
  flag('--model') ?? (hasKey('ANTHROPIC_API_KEY') ? 'anthropic:claude-sonnet-5-5' : 'ollama:qwen3:8b')

const COMMANDS = [
  ['help', 'Show commands and keyboard shortcuts'],
  ['model', 'Switch model, e.g. /model ollama:qwen3:8b or /model anthropic:claude-sonnet-5-5'],
  ['mode', 'Show or set the mode: default, accept-edits, plan, yolo'],
  ['effort', "Set Claude's reasoning effort: low, medium, high, xhigh, max"],
  ['think', 'Turn reasoning on/off for local models, and show/hide thinking: /think on|off'],
  ['compact', 'Summarize the conversation to free up context: /compact [what to focus on]'],
  ['clear', 'Start a fresh conversation'],
  ['context', 'Visualize how full the context window is'],
  ['cost', 'Show token usage and estimated cost for this session'],
  ['todos', 'Show the current todo list'],
  ['init', 'Analyze this project and write an AGENTS.md memory file'],
  ['memory', 'Show which memory files are loaded'],
  ['status', 'Show the current model, mode and settings'],
  ['review', 'custom command']
].map(([name, description]) => ({ name, description }))

const MCP = [
  { name: 'github', status: 'connected', tools: ['search_issues', 'get_issue', 'create_pull_request', 'get_file_contents'] },
  { name: 'postgres', status: 'failed', tools: [], error: 'Connection refused (localhost:5432). Is the database running?' },
  { name: 'linear', status: 'disabled', tools: [] }
]

// ------------------------------------------------------------------ state

const LOGGED = new Set(['user', 'assistant', 'tool.call', 'tool.result', 'todos', 'info', 'warn', 'error', 'compacted', 'interrupted'])
const MODES = ['default', 'accept-edits', 'plan']
let model = pickDefault()
const status = { mode: 'default', model, busy: false, context_used: 0, context_limit: 32768 }
let effort = process.env.NANOCODE_EFFORT || 'high'
let think = process.env.NANOCODE_THINK !== '0'
const usage = {} // spec -> {input, cached, output}
let alwaysEdits = false
const alwaysCommands = new Set()
let ids = { turn: 0, call: 0, req: 0, session: 0 }
const sessions = new Map()
let current = null
let turn = null // {id, abort, waiters: Map}

function windowFor(spec) {
  return MODELS.find((m) => m.spec === spec)?.window ?? 32768
}
status.context_limit = Math.min(windowFor(model), 32768)
const SYSTEM_TOKENS = 2140
const TOOL_TOKENS = 1730

function newSession(title = 'New session', created = now()) {
  const id = `s${Date.now().toString(36)}${++ids.session}`
  const s = { info: { id, title, created, updated: created, model, turns: 0 }, log: [], todos: [], checkpoints: [], tokens: 0 }
  sessions.set(id, s)
  return s
}

function seed() {
  const day = 86400
  const a = newSession('Add coupon expiry dates', now() - 2 * day - 3600)
  a.info.turns = 2
  a.info.model = 'anthropic:claude-sonnet-5-5'
  a.tokens = 9400
  a.log = [
    { event: 'user', turn_id: 'old1', text: 'Coupons should be able to expire. Add an optional expiry date.' },
    { event: 'assistant', text: "I'll add an `expires` field to each coupon and check it in `apply_coupon`.", thinking: '', thinking_seconds: 0 },
    { event: 'tool.call', call_id: 'old-c1', name: 'read_file', label: 'Read', arg: 'cart.py', args: { path: 'cart.py' } },
    { event: 'tool.result', call_id: 'old-c1', name: 'read_file', summary: 'Read 33 lines', detail: '', detail_kind: '', is_error: false },
    { event: 'assistant', text: 'Done. `apply_coupon` now raises `ValueError("coupon expired")` for expired codes.', thinking: '', thinking_seconds: 0 }
  ]
  const b = newSession('Explain the cart module', now() - day + 7200)
  b.info.turns = 1
  b.tokens = 5200
  b.log = [
    { event: 'user', turn_id: 'old2', text: 'explain cart.py to me' },
    { event: 'assistant', text: '`cart.py` defines a tiny `Cart` class: items are stored as `name -> {price, qty}`, and totals apply a coupon discount and then 8% tax.', thinking: '', thinking_seconds: 0 }
  ]
}

function emit(obj) {
  if (LOGGED.has(obj.event) && current) current.log.push(obj)
  out(obj)
}
function sendStatus() {
  status.model = model
  status.context_used = current ? SYSTEM_TOKENS + TOOL_TOKENS + current.tokens : 0
  out({ event: 'status', ...status })
}
function touch() {
  current.info.updated = now()
  current.info.model = model
  out({ event: 'session', session: current.info })
}

class Aborted extends Error {}
function abortable(start) {
  return new Promise((resolve, reject) => {
    const signal = turn?.abort.signal
    if (signal?.aborted) return reject(new Aborted())
    let cleanup = () => {}
    const onAbort = () => {
      cleanup()
      reject(new Aborted())
    }
    cleanup = start((value) => {
      signal?.removeEventListener('abort', onAbort)
      resolve(value)
    })
    signal?.addEventListener('abort', onAbort, { once: true })
  })
}
function sleep(ms) {
  return abortable((done) => {
    const t = setTimeout(done, ms / SPEED)
    return () => clearTimeout(t)
  })
}
function waitFor(requestId) {
  return abortable((done) => {
    turn.waiters.set(requestId, done)
    return () => turn?.waiters.delete(requestId)
  })
}

async function streamText(text, parent, kind = 'text') {
  const parts = text.match(/\S+\s*|\s+/g) ?? []
  let chunk = ''
  for (let i = 0; i < parts.length; i++) {
    chunk += parts[i]
    if (chunk.length > 10 || i === parts.length - 1) {
      out({ event: kind, delta: chunk, ...(parent ? { parent } : {}) })
      chunk = ''
      await sleep(kind === 'thinking' ? 18 : 26)
    }
  }
}

/** One model call: optional thinking, optional streamed text, then the `assistant` event. */
async function modelCall({ text = '', thinking = '', parent, tokens = 900, tools = [] }) {
  out({ event: 'stream.start', ...(parent ? { parent } : {}) })
  await sleep(350)
  const started = Date.now()
  if (thinking && think) await streamText(thinking, parent, 'thinking')
  const seconds = Math.round((Date.now() - started) / 1000)
  if (text) await streamText(text, parent)
  for (const name of tools) {
    out({ event: 'tool.preparing', name, ...(parent ? { parent } : {}) })
    await sleep(260)
  }
  emit({ event: 'assistant', text, thinking: think ? thinking : '', thinking_seconds: seconds, ...(parent ? { parent } : {}) })
  const u = (usage[model] ??= { input: 0, cached: 0, output: 0 })
  u.input += 2400 + current.tokens
  u.cached += Math.max(0, current.tokens - 800)
  u.output += Math.round(tokens / 3)
  if (!parent) {
    current.tokens += tokens
    sendStatus()
  }
}

const LABELS = { read_file: 'Read', write_file: 'Write', edit_file: 'Update', bash: 'Bash', glob: 'Glob', grep: 'Grep', todo_write: 'Update Todos', exit_plan_mode: 'Plan', task: 'Task', web_fetch: 'Fetch' }

function toolCall(name, arg, args, parent) {
  const call_id = `c${++ids.call}`
  emit({ event: 'tool.call', call_id, name, label: LABELS[name] ?? name, arg, args, ...(parent ? { parent } : {}) })
  return call_id
}
function toolResult(call_id, name, { summary = '', detail = '', detail_kind = '', is_error = false, parent } = {}) {
  emit({ event: 'tool.result', call_id, name, summary, detail, detail_kind, is_error, ...(parent ? { parent } : {}) })
}

/** Ask for permission like the real engine. Returns {answer, feedback}. */
async function permission(call_id, name, { title, question, preview, preview_kind, always_label }) {
  const request_id = `p${++ids.req}`
  out({ event: 'permission.request', request_id, call_id, name, title, question, preview, preview_kind, always_label })
  return waitFor(request_id)
}

function commandRule(cmd) {
  const words = cmd.trim().split(/\s+/)
  return words[0] === 'python' && words[1] === '-m' ? words.slice(0, 3).join(' ') : words[0]
}

const READ_ONLY = /^(ls|cat|pwd|git status|git diff|git log|head|tail|wc|rg|grep)\b/

async function runBash(command, output, { parent, isError = false } = {}) {
  const call_id = toolCall('bash', command, { command }, parent)
  if (!READ_ONLY.test(command) && status.mode !== 'yolo' && !alwaysCommands.has(commandRule(command))) {
    const { answer, feedback } = await permission(call_id, 'bash', {
      title: 'Bash command',
      question: 'Do you want to run this command?',
      preview: command,
      preview_kind: 'command',
      always_label: `Yes, and don't ask again for \`${commandRule(command)}\` commands this session`
    })
    if (answer === 'no') return reject(call_id, 'bash', feedback, parent)
    if (answer === 'always') alwaysCommands.add(commandRule(command))
  }
  await sleep(700)
  toolResult(call_id, 'bash', { summary: isError ? 'Exit code 1' : '', detail: output, detail_kind: 'text', is_error: false, parent })
  return { ok: true }
}

function diffFor(file, line, before, after, contextBefore = [], contextAfter = []) {
  const oldLen = contextBefore.length + before.length + contextAfter.length
  const newLen = contextBefore.length + after.length + contextAfter.length
  const start = line - contextBefore.length
  return [
    `--- ${file}`,
    `+++ ${file}`,
    `@@ -${start},${oldLen} +${start},${newLen} @@`,
    ...contextBefore.map((l) => ' ' + l),
    ...before.map((l) => '-' + l),
    ...after.map((l) => '+' + l),
    ...contextAfter.map((l) => ' ' + l)
  ].join('\n')
}

async function edit(file, diff, { adds, removes, parent, create = false }) {
  const name = create ? 'write_file' : 'edit_file'
  const call_id = toolCall(name, file, create ? { path: file, content: '…' } : { path: file, old_string: '…', new_string: '…' }, parent)
  if (status.mode === 'default' && !alwaysEdits) {
    const { answer, feedback } = await permission(call_id, name, {
      title: create ? 'Create file' : 'Edit file',
      question: create ? `Do you want to create ${file}?` : `Do you want to make this edit to ${file}?`,
      preview: diff,
      preview_kind: 'diff',
      always_label: 'Yes, allow all edits during this session (shift+tab)'
    })
    if (answer === 'no') return reject(call_id, name, feedback, parent)
    if (answer === 'always') {
      alwaysEdits = true
      status.mode = 'accept-edits'
      sendStatus()
    }
  }
  await sleep(250)
  const plural = (n, w) => `${n} ${w}${n === 1 ? '' : 's'}`
  const summary = create
    ? `Wrote ${plural(adds, 'line')} to ${file}`
    : `Updated ${file} with ${plural(adds, 'addition')} and ${plural(removes, 'removal')}`
  toolResult(call_id, name, { summary, detail: diff, detail_kind: 'diff', parent })
  turn.files.add(file)
  return { ok: true }
}

function reject(call_id, name, feedback, parent) {
  toolResult(call_id, name, {
    summary: feedback ? `The user said no: ${feedback}` : 'The user declined this action.',
    is_error: true,
    parent
  })
  return { ok: false, feedback }
}

async function todos(list) {
  const call_id = toolCall('todo_write', '', { todos: list })
  await sleep(120)
  current.todos = list
  toolResult(call_id, 'todo_write', { summary: `${list.filter((t) => t.status === 'completed').length}/${list.length} completed`, detail: '', detail_kind: 'todos' })
  emit({ event: 'todos', todos: list })
}

// ------------------------------------------------------------------ scripted turns

const UNITTEST_FAIL = `test_adding_same_item_increases_quantity (test_cart.CartTests) ... ok
test_coupon_takes_a_percentage_off (test_cart.CartTests) ... FAIL
test_removing_a_missing_item_raises_value_error (test_cart.CartTests) ... ERROR
test_subtotal_counts_quantity (test_cart.CartTests) ... FAIL
test_total_includes_tax (test_cart.CartTests) ... FAIL
test_unknown_coupon_is_rejected (test_cart.CartTests) ... ok

======================================================================
ERROR: test_removing_a_missing_item_raises_value_error (test_cart.CartTests)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "test_cart.py", line 36, in test_removing_a_missing_item_raises_value_error
    Cart().remove_item("ghost")
  File "cart.py", line 21, in remove_item
    del self.items[name]
KeyError: 'ghost'

======================================================================
FAIL: test_coupon_takes_a_percentage_off (test_cart.CartTests)
----------------------------------------------------------------------
AssertionError: -972.0 != 97.2

======================================================================
FAIL: test_subtotal_counts_quantity (test_cart.CartTests)
----------------------------------------------------------------------
AssertionError: 3.5 != 5.0

======================================================================
FAIL: test_total_includes_tax (test_cart.CartTests)
----------------------------------------------------------------------
AssertionError: 21.6 != 43.2

----------------------------------------------------------------------
Ran 6 tests in 0.001s

FAILED (failures=3, errors=1)`

const UNITTEST_OK = `test_adding_same_item_increases_quantity (test_cart.CartTests) ... ok
test_coupon_takes_a_percentage_off (test_cart.CartTests) ... ok
test_removing_a_missing_item_raises_value_error (test_cart.CartTests) ... ok
test_subtotal_counts_quantity (test_cart.CartTests) ... ok
test_total_includes_tax (test_cart.CartTests) ... ok
test_unknown_coupon_is_rejected (test_cart.CartTests) ... ok

----------------------------------------------------------------------
Ran 6 tests in 0.001s

OK`

async function fixTestsTurn() {
  await modelCall({
    thinking:
      "The user says the tests are failing. Before changing anything I should run the test suite to see exactly which tests fail and how, then read cart.py and test_cart.py. The tests are the spec here, so I should fix the code, not the tests.",
    text: "I'll start by running the test suite to see what's failing.",
    tools: ['bash'],
    tokens: 700
  })
  const r1 = await runBash('python -m unittest -v', UNITTEST_FAIL, { isError: true })
  if (!r1.ok) return afterNo(r1)

  await modelCall({ text: 'Four of the six tests fail. Let me plan this out and read the code.', tools: ['todo_write'], tokens: 1200 })
  await todos([
    { content: 'Read cart.py and the failing tests', status: 'in_progress' },
    { content: 'Fix subtotal to count quantity', status: 'pending' },
    { content: 'Fix the coupon percentage', status: 'pending' },
    { content: 'Raise ValueError for missing items', status: 'pending' },
    { content: 'Run the tests to confirm', status: 'pending' }
  ])

  await modelCall({ tools: ['read_file', 'read_file'], tokens: 300 })
  const read1 = toolCall('read_file', 'cart.py', { path: 'cart.py' })
  const read2 = toolCall('read_file', 'test_cart.py', { path: 'test_cart.py' })
  await sleep(180)
  toolResult(read1, 'read_file', { summary: 'Read 33 lines' })
  await sleep(90)
  toolResult(read2, 'read_file', { summary: 'Read 41 lines' })
  current.tokens += 1500

  // A subagent explores in parallel while the main agent waits.
  await modelCall({
    thinking: 'The coupon bug looks like a percent vs fraction mix-up. I want to be sure COUPONS is not used as a fraction somewhere else, so I will hand that search to a subagent.',
    text: '',
    tools: ['task'],
    tokens: 600
  })
  const task = toolCall('task', 'Check how coupon percentages are used', {
    description: 'Check how coupon percentages are used',
    prompt: 'Find every place COUPONS or discount_percent is used in this project and report whether values are whole percentages (10) or fractions (0.1).'
  })
  await sleep(300)
  await modelCall({ parent: task, text: "I'll search for every use of the coupon table and the discount field.", tools: ['grep'] })
  const g = toolCall('grep', '"COUPONS|discount_percent"', { pattern: 'COUPONS|discount_percent' }, task)
  await sleep(400)
  toolResult(g, 'grep', { summary: 'Found 5 matches in 1 file', detail: 'cart.py:4:COUPONS = {"SAVE10": 10, "HALF": 50}\ncart.py:10:        self.discount_percent = 0\ncart.py:24:        if code not in COUPONS:\ncart.py:26:        self.discount_percent = COUPONS[code]\ncart.py:29:        discounted = self.subtotal() * (1 - self.discount_percent)', detail_kind: 'text', parent: task })
  await modelCall({ parent: task, tools: ['read_file'] })
  const r = toolCall('read_file', 'cart.py, lines 20+', { path: 'cart.py', offset: 20 }, task)
  await sleep(250)
  toolResult(r, 'read_file', { summary: 'Read 14 lines', parent: task })
  const report =
    'COUPONS stores **whole-number percentages** (`SAVE10: 10`, `HALF: 50`). `total()` subtracts the raw value: `1 - self.discount_percent` gives `1 - 10 = -9`, which explains `-972.0`. It should be `1 - self.discount_percent / 100`. Nothing else reads these values.'
  await modelCall({ parent: task, text: report })
  toolResult(task, 'task', { summary: 'Done (2 tool uses)', detail: '', detail_kind: '' })
  current.tokens += 900

  await todos([
    { content: 'Read cart.py and the failing tests', status: 'completed' },
    { content: 'Fix subtotal to count quantity', status: 'in_progress' },
    { content: 'Fix the coupon percentage', status: 'pending' },
    { content: 'Raise ValueError for missing items', status: 'pending' },
    { content: 'Run the tests to confirm', status: 'pending' }
  ])
  await modelCall({
    thinking: 'subtotal() sums item["price"] but ignores qty. apple 0.5*4 + bread 3.0 = 5.0 expected, the code gives 3.5. Fix: multiply by qty.',
    text: 'Found three bugs. First, `subtotal()` ignores the quantity:',
    tools: ['edit_file'],
    tokens: 800
  })
  const e1 = await edit(
    'cart.py',
    diffFor('cart.py', 24, ['        return sum(item["price"] for item in self.items.values())'], ['        return sum(item["price"] * item["qty"] for item in self.items.values())'], ['    def subtotal(self):'], ['', '    def apply_coupon(self, code):']),
    { adds: 1, removes: 1 }
  )
  if (!e1.ok) return afterNo(e1)

  await todos([
    { content: 'Read cart.py and the failing tests', status: 'completed' },
    { content: 'Fix subtotal to count quantity', status: 'completed' },
    { content: 'Fix the coupon percentage', status: 'in_progress' },
    { content: 'Raise ValueError for missing items', status: 'pending' },
    { content: 'Run the tests to confirm', status: 'pending' }
  ])
  await modelCall({ text: 'Next, the coupon is a whole percentage, so it has to be divided by 100:', tools: ['edit_file'], tokens: 600 })
  const e2 = await edit(
    'cart.py',
    diffFor('cart.py', 32, ['        discounted = self.subtotal() * (1 - self.discount_percent)'], ['        discounted = self.subtotal() * (1 - self.discount_percent / 100)'], ['    def total(self):'], ['        return round(discounted * (1 + TAX_RATE), 2)']),
    { adds: 1, removes: 1 }
  )
  if (!e2.ok) return afterNo(e2)

  await modelCall({ text: 'And removing a missing item should raise `ValueError`, not `KeyError`:', tools: ['edit_file'], tokens: 600 })
  const e3 = await edit(
    'cart.py',
    diffFor('cart.py', 21, ['        del self.items[name]'], ['        if name not in self.items:', '            raise ValueError(f"not in cart: {name}")', '        del self.items[name]'], ['    def remove_item(self, name):'], ['']),
    { adds: 2, removes: 0 }
  )
  if (!e3.ok) return afterNo(e3)

  await todos([
    { content: 'Read cart.py and the failing tests', status: 'completed' },
    { content: 'Fix subtotal to count quantity', status: 'completed' },
    { content: 'Fix the coupon percentage', status: 'completed' },
    { content: 'Raise ValueError for missing items', status: 'completed' },
    { content: 'Run the tests to confirm', status: 'in_progress' }
  ])
  await modelCall({ text: 'Now let me run the tests again.', tools: ['bash'], tokens: 300 })
  const r2 = await runBash('python -m unittest -v', UNITTEST_OK)
  if (!r2.ok) return afterNo(r2)
  await todos([
    { content: 'Read cart.py and the failing tests', status: 'completed' },
    { content: 'Fix subtotal to count quantity', status: 'completed' },
    { content: 'Fix the coupon percentage', status: 'completed' },
    { content: 'Raise ValueError for missing items', status: 'completed' },
    { content: 'Run the tests to confirm', status: 'completed' }
  ])
  await modelCall({
    text: `All **6 tests pass** now. There were three bugs in \`cart.py\`, all in the code rather than the tests:

| Test | Cause | Fix |
|---|---|---|
| \`test_subtotal_counts_quantity\` | \`subtotal()\` ignored \`qty\` | multiply \`price * qty\` |
| \`test_coupon_takes_a_percentage_off\` | \`SAVE10\` subtracted **10×** the price | divide the percentage by 100 |
| \`test_removing_a_missing_item_…\` | \`del\` raised \`KeyError\` | raise \`ValueError\` first |

The coupon fix in context:

\`\`\`python
def total(self):
    discounted = self.subtotal() * (1 - self.discount_percent / 100)
    return round(discounted * (1 + TAX_RATE), 2)
\`\`\`

\`test_total_includes_tax\` was failing only because of the subtotal bug, so it passes without a separate change.`,
    tokens: 1400
  })
}

async function afterNo({ feedback }) {
  if (!feedback) return
  await modelCall({ text: `Understood: ${feedback}. I'll leave the code as it is. Let me know how you'd like to proceed.`, tokens: 200 })
}

async function explainTurn() {
  await modelCall({ thinking: 'Look at the layout first, then read the main module.', text: "I'll take a look around the project.", tools: ['glob'] })
  const g = toolCall('glob', '**/*', { pattern: '**/*' })
  await sleep(200)
  const files = walk().slice(0, 40)
  toolResult(g, 'glob', { summary: `Found ${files.length} files`, detail: files.join('\n'), detail_kind: 'text' })
  await modelCall({ tools: ['read_file'] })
  const target = files.find((f) => f.endsWith('.py')) ?? files[0] ?? 'README.md'
  const r = toolCall('read_file', target, { path: target })
  await sleep(200)
  toolResult(r, 'read_file', { summary: 'Read 33 lines' })
  await modelCall({
    text: `This is a small Python project (${files.length} files). Here's the shape of it:

- **\`cart.py\`** defines \`Cart\`, which stores items as \`name -> {"price", "qty"}\` and computes \`subtotal()\` and \`total()\` (coupon discount, then 8% tax).
- **\`test_cart.py\`** has six \`unittest\` tests that describe the intended behaviour.
- \`COUPONS\` maps codes to **whole-number percentages**, e.g. \`"SAVE10": 10\`.

Run the tests with:

\`\`\`bash
python -m unittest -v
\`\`\`

> Tip: ask me to *find and fix the failing tests* and I'll walk through it step by step.`,
    tokens: 1800
  })
}

async function planTurn(text) {
  if (status.mode !== 'plan') {
    status.mode = 'plan'
    sendStatus()
  }
  await modelCall({ thinking: 'Plan mode: research read-only, then propose a plan with exit_plan_mode.', text: "I'll research the code first (plan mode is read-only).", tools: ['read_file'] })
  const r = toolCall('read_file', 'cart.py', { path: 'cart.py' })
  await sleep(250)
  toolResult(r, 'read_file', { summary: 'Read 33 lines' })
  const plan = `## Plan: ${text.replace(/^plan[:\s]*/i, '').slice(0, 60) || 'fix the cart'}

1. **\`cart.py\`**: make \`subtotal()\` multiply \`price * qty\`.
2. **\`cart.py\`**: divide \`discount_percent\` by 100 in \`total()\`.
3. **\`cart.py\`**: raise \`ValueError\` in \`remove_item()\` when the item is missing.
4. Run \`python -m unittest -v\` and confirm all 6 tests pass.

No test files will change.`
  await modelCall({ tools: ['exit_plan_mode'] })
  const call = toolCall('exit_plan_mode', '', { plan })
  const request_id = `p${++ids.req}`
  out({ event: 'plan.request', request_id, plan })
  const { choice, feedback } = await waitFor(request_id)
  if (choice === 'no') {
    toolResult(call, 'exit_plan_mode', { summary: feedback ? `Keep planning: ${feedback}` : 'The user wants to keep planning.', is_error: true })
    if (feedback) await modelCall({ text: `Got it. I'll revise the plan: ${feedback}`, tokens: 200 })
    return
  }
  status.mode = choice
  sendStatus()
  toolResult(call, 'exit_plan_mode', { summary: `Plan approved (${choice === 'accept-edits' ? 'auto-accept edits' : 'approve edits manually'})` })
  await modelCall({ text: 'Plan approved. Starting with the subtotal.', tools: ['edit_file'] })
  const e = await edit(
    'cart.py',
    diffFor('cart.py', 24, ['        return sum(item["price"] for item in self.items.values())'], ['        return sum(item["price"] * item["qty"] for item in self.items.values())'], ['    def subtotal(self):'], []),
    { adds: 1, removes: 1 }
  )
  if (!e.ok) return afterNo(e)
  await modelCall({ text: 'Step 1 is done. The rest of the plan follows the same pattern.', tokens: 300 })
}

async function errorTurn() {
  await modelCall({ text: 'Let me check that…' })
  emit({ event: 'error', message: 'Ollama returned HTTP 500: model runner has unexpectedly stopped (out of memory?). Try a smaller model or a shorter context.' })
}

async function longTurn() {
  await modelCall({ text: 'This will take a while. I am going to run the full build.', tools: ['bash'] })
  const call_id = toolCall('bash', 'sleep 30 && make all', { command: 'sleep 30 && make all' })
  turn.running.add(call_id)
  await sleep(30000)
  toolResult(call_id, 'bash', { detail: 'done', detail_kind: 'text' })
}

async function initTurn() {
  await modelCall({ text: "I'll look at the project and write an AGENTS.md.", tools: ['glob'] })
  const g = toolCall('glob', '**/*', { pattern: '**/*' })
  await sleep(200)
  toolResult(g, 'glob', { summary: `Found ${walk().length} files` })
  await modelCall({ tools: ['write_file'] })
  const content = ['# AGENTS.md', '', 'A tiny shopping cart in plain Python.', '', '- Run tests: `python -m unittest -v`', '- Fix the code, never the tests.', '- No third-party dependencies.']
  const diff = ['--- AGENTS.md', '+++ AGENTS.md', `@@ -0,0 +1,${content.length} @@`, ...content.map((l) => '+' + l)].join('\n')
  const e = await edit('AGENTS.md', diff, { adds: content.length, removes: 0, create: true })
  if (!e.ok) return afterNo(e)
  await modelCall({ text: 'Wrote `AGENTS.md`. It will be loaded at the start of every session.' })
}

async function compactNow(instructions = '', auto = false) {
  const before = SYSTEM_TOKENS + TOOL_TOKENS + current.tokens
  if (auto) emit({ event: 'info', message: `Context is ${Math.round((100 * before) / status.context_limit)}% full, compacting the conversation…` })
  out({ event: 'stream.start' })
  await sleep(1400)
  current.tokens = 1100
  const after = SYSTEM_TOKENS + TOOL_TOKENS + current.tokens
  emit({
    event: 'compacted',
    before,
    after,
    limit: status.context_limit,
    summary: `## Summary of the conversation so far${instructions ? ` (focus: ${instructions})` : ''}

- The user asked to fix the failing tests in \`demo/shop\` without changing the tests.
- Found three bugs in \`cart.py\`: subtotal ignored \`qty\`, the coupon percentage was not divided by 100, and \`remove_item\` raised \`KeyError\`.
- All three were fixed and \`python -m unittest -v\` now passes (6 tests).
- Open question: whether coupons should be able to expire.`
  })
  sendStatus()
}

const HELP = () =>
  'Commands: ' + COMMANDS.map((c) => '/' + c.name).join(' · ') + '\nShortcuts: ! <command> runs a shell command · # <note> saves to AGENTS.md · @<path> attaches a file · shift+tab cycles modes'

async function slash(text) {
  const [name, ...rest] = text.slice(1).split(' ')
  const arg = rest.join(' ').trim()
  switch (name) {
    case 'help':
      emit({ event: 'info', message: HELP() })
      return
    case 'compact':
      return compactNow(arg)
    case 'clear':
      current = newSession()
      out({ event: 'session', session: current.info })
      emit({ event: 'info', message: 'Started a new conversation (project memory reloaded)' })
      sendStatus()
      return
    case 'context':
      emit({ event: 'info', message: `Context: ${(status.context_used / 1000).toFixed(1)}k / ${(status.context_limit / 1000).toFixed(1)}k tokens` })
      return
    case 'cost':
      emit({ event: 'info', message: Object.keys(usage).length ? Object.keys(usage).map((m) => `${m}: ${costString(m)}`).join(' · ') : 'No model calls yet.' })
      return
    case 'init':
      return initTurn()
    case 'model':
      if (!arg) return emit({ event: 'info', message: `Current model: ${model}` })
      try {
        setModel(arg)
        emit({ event: 'info', message: `Now using ${model} · context budget ${(status.context_limit / 1000).toFixed(1)}k tokens` })
      } catch (e) {
        emit({ event: 'error', message: e.message })
      }
      return
    case 'mode':
      if (!arg) return emit({ event: 'info', message: `Mode: ${status.mode}  (options: default, accept-edits, plan, yolo · shift+tab cycles)` })
      status.mode = arg
      sendStatus()
      return emit({ event: 'info', message: `Mode set to ${arg}` })
    case 'effort':
      if (arg) effort = arg
      return emit({ event: 'info', message: `Effort set to ${effort}` })
    case 'think':
      if (arg) think = arg === 'on'
      return emit({ event: 'info', message: `Thinking ${think ? 'on' : 'off'}` })
    case 'todos':
      return emit({ event: 'info', message: current.todos.length ? current.todos.map((t) => `${t.status === 'completed' ? '☒' : '☐'} ${t.content}`).join('\n') : 'No todos yet.' })
    case 'memory':
      return emit({ event: 'info', message: 'No memory files loaded. Run /init to create AGENTS.md, or add a note with: # <note>' })
    case 'status':
      return emit({ event: 'info', message: `model ${model} · mode ${status.mode} · effort ${effort} · thinking ${think ? 'on' : 'off'} · cwd ${CWD}` })
    case 'review':
      return explainTurn()
    default:
      emit({ event: 'error', message: `Unknown command /${name}. Type /help to see what's available.` })
  }
}

async function runTurn(text) {
  if (text.startsWith('/')) return slash(text)
  if (text.startsWith('!')) {
    const command = text.slice(1).trim()
    const call_id = toolCall('bash', command, { command })
    await sleep(300)
    const listing = command.startsWith('ls') ? walk().filter((f) => !f.includes('/')).join('\n') : `$ ${command}\n(mock engine: command not actually run)`
    toolResult(call_id, 'bash', { detail: listing, detail_kind: 'text' })
    return
  }
  if (text.startsWith('#')) {
    emit({ event: 'info', message: "Saved to AGENTS.md - it's loaded automatically in every future session" })
    return
  }
  const lower = text.toLowerCase()
  const mentions = [...text.matchAll(/(?<![\w@])@([\w./~-]+)/g)].map((m) => m[1])
  for (const m of mentions) emit({ event: 'info', message: `Attached ${m}` })
  if (status.mode === 'plan' || /^plan\b/.test(lower)) return planTurn(text)
  if (lower.includes('explain') || lower.includes('what does')) return explainTurn()
  if (lower.includes('error')) return errorTurn()
  if (lower.includes('long')) return longTurn()
  if (/\b(hi|hello|hey|thanks)\b/.test(lower) && text.length < 30) {
    return modelCall({ text: "Hi! I'm nanocode. Ask me to explain this project, fix a bug, or write a feature.", tokens: 120 })
  }
  return fixTestsTurn()
}

async function startTurn(text) {
  const turn_id = `t${++ids.turn}`
  turn = { id: turn_id, abort: new AbortController(), waiters: new Map(), files: new Set(), running: new Set(), prompt: text }
  const isCommand = text.startsWith('/') && !['/init', '/review'].includes(text.split(' ')[0])
  emit({ event: 'user', turn_id, text })
  status.busy = true
  sendStatus()
  if (!isCommand) {
    current.info.turns += 1
    if (current.info.title === 'New session') current.info.title = text.replace(/\s+/g, ' ').slice(0, 48)
    touch()
  }
  try {
    await runTurn(text)
    if (status.context_used / status.context_limit > 0.85) await compactNow('', true)
  } catch (e) {
    if (e instanceof Aborted) {
      for (const id of turn.running) toolResult(id, 'bash', { summary: 'Interrupted', is_error: true })
      emit({ event: 'interrupted' })
    } else {
      logErr(e.stack)
      emit({ event: 'error', message: String(e.message || e) })
    }
  }
  let checkpoint = null
  if (turn.files.size) {
    checkpoint = { turn_id, prompt: text, created: now(), files: [...turn.files] }
    current.checkpoints.push({ ...checkpoint, logIndex: current.log.findIndex((ev) => ev.event === 'user' && ev.turn_id === turn_id) })
  }
  out({ event: 'turn.end', turn_id, checkpoint })
  turn = null
  status.busy = false
  sendStatus()
}

// ------------------------------------------------------------------ helpers

function walk() {
  const files = []
  const skip = new Set(['node_modules', '.git', '.venv', '__pycache__', 'out', 'release', 'dist', '.DS_Store'])
  const visit = (dir, rel) => {
    let entries = []
    try {
      entries = fs.readdirSync(dir, { withFileTypes: true })
    } catch {
      return
    }
    for (const e of entries) {
      if (skip.has(e.name) || files.length >= 5000) continue
      const r = rel ? `${rel}/${e.name}` : e.name
      if (e.isDirectory()) visit(path.join(dir, e.name), r)
      else files.push(r)
    }
  }
  visit(CWD, '')
  return files
}

function setModel(spec) {
  const m = MODELS.find((x) => x.spec === spec)
  if (m && !available(m)) throw new Error(`${m.key} is not set. Add it in Settings → API Keys (or your shell / .env), then try again.`)
  if (!m && !spec.includes(':')) throw new Error(`Can't reach ${spec}: use provider:model, e.g. ollama:qwen3:8b`)
  model = spec
  status.context_limit = Math.min(windowFor(spec), spec.startsWith('ollama:') ? 32768 : 200000)
  sendStatus()
}

function costString(spec) {
  const u = usage[spec]
  if (spec.startsWith('ollama:')) return '$0.00 (local)'
  const prices = { 'anthropic:claude-opus-5-5': [4, 20, 0.2], 'anthropic:claude-sonnet-5-5': [2, 10, 0.2], 'anthropic:claude-haiku-5-5': [0.1, 0.5, 0.01] }[spec]
  if (!prices) return 'not tracked'
  return '$' + (((u.input - u.cached) * prices[0] + u.output * prices[1] + u.cached * prices[2]) / 1e6).toFixed(4)
}

// ------------------------------------------------------------------ requests

const handlers = {
  initialize() {
    current = newSession()
    sendStatus()
    return {
      version: VERSION,
      cwd: CWD,
      status: { ...status },
      effort,
      think,
      session: current.info,
      sessions: [...sessions.values()].map((s) => s.info).sort((a, b) => b.updated - a.updated),
      commands: COMMANDS,
      mcp: MCP,
      keys: Object.fromEntries(KEYS.map((k) => [k, hasKey(k)]))
    }
  },
  prompt({ text }) {
    if (turn) throw new Error('A turn is already running.')
    const turn_id = `t${ids.turn + 1}`
    setImmediate(() => void startTurn(text))
    return { turn_id }
  },
  interrupt() {
    turn?.abort.abort()
    return {}
  },
  'permission.respond'({ request_id, answer, feedback }) {
    turn?.waiters.get(request_id)?.({ answer, feedback })
    return {}
  },
  'plan.respond'({ request_id, choice, feedback }) {
    turn?.waiters.get(request_id)?.({ choice, feedback })
    return {}
  },
  'mode.set'({ mode }) {
    status.mode = mode
    sendStatus()
    return { status: { ...status } }
  },
  'mode.cycle'() {
    status.mode = MODES[(MODES.indexOf(status.mode) + 1) % MODES.length]
    sendStatus()
    return { status: { ...status } }
  },
  'models.list'() {
    return {
      current: model,
      options: MODELS.map((m) => ({ spec: m.spec, label: m.label, detail: m.detail, group: m.group, available: available(m), ...(available(m) ? {} : { needs: m.key }) }))
    }
  },
  'model.set'({ spec }) {
    setModel(spec)
    return { status: { ...status } }
  },
  'effort.set'({ effort: e }) {
    effort = e
    return {}
  },
  'think.set'({ on }) {
    think = on
    return {}
  },
  compact({ instructions }) {
    if (turn) throw new Error('A turn is already running.')
    turn = { id: 'compact', abort: new AbortController(), waiters: new Map(), files: new Set(), running: new Set() }
    status.busy = true
    sendStatus()
    setImmediate(async () => {
      try {
        await compactNow(instructions)
      } catch {
        emit({ event: 'interrupted' })
      }
      turn = null
      status.busy = false
      sendStatus()
    })
    return {}
  },
  'context.get'() {
    const used = SYSTEM_TOKENS + TOOL_TOKENS + current.tokens
    return { used, limit: status.context_limit, window: windowFor(model), system: SYSTEM_TOKENS, tools: TOOL_TOKENS, messages: current.tokens, microcompact_at: 0.6, compact_at: 0.85 }
  },
  'cost.get'() {
    return { rows: Object.entries(usage).map(([m, u]) => ({ model: m, input: u.input, cached: u.cached, output: u.output, cost: costString(m) })) }
  },
  'todos.get'() {
    return { todos: current.todos }
  },
  'session.list'() {
    return { sessions: [...sessions.values()].map((s) => s.info).sort((a, b) => b.updated - a.updated) }
  },
  'session.new'() {
    if (turn) throw new Error('A turn is already running.')
    current = newSession()
    sendStatus()
    return { session: current.info }
  },
  'session.load'({ session_id }) {
    if (turn) throw new Error("Can't switch sessions while a turn is running.")
    const s = sessions.get(session_id)
    if (!s) throw new Error(`No session ${session_id}`)
    current = s
    sendStatus()
    return { session: s.info, transcript: s.log, todos: s.todos, status: { ...status } }
  },
  'session.rename'({ session_id, title }) {
    const s = sessions.get(session_id)
    if (!s) throw new Error(`No session ${session_id}`)
    s.info.title = title
    return { session: s.info }
  },
  'session.delete'({ session_id }) {
    if (current?.info.id === session_id) throw new Error("Can't delete the open session.")
    sessions.delete(session_id)
    return {}
  },
  'checkpoint.list'() {
    return { checkpoints: current.checkpoints.map(({ logIndex, ...c }) => c) }
  },
  'checkpoint.restore'({ turn_id, conversation }) {
    if (turn) throw new Error('A turn is already running.')
    const i = current.checkpoints.findIndex((c) => c.turn_id === turn_id)
    if (i < 0) throw new Error(`No checkpoint for ${turn_id}`)
    const later = current.checkpoints.slice(i)
    const restored = [...new Set(later.flatMap((c) => c.files))]
    let transcript
    if (conversation) {
      current.log = current.log.slice(0, Math.max(0, later[0].logIndex))
      current.checkpoints = current.checkpoints.slice(0, i)
      current.tokens = Math.max(0, Math.round(current.tokens * 0.4))
      transcript = current.log
    }
    sendStatus()
    return { restored, ...(transcript ? { transcript } : {}), status: { ...status } }
  },
  'files.search'({ query = '', limit = 30 }) {
    const needle = query.toLowerCase()
    const hits = walk().filter((p) => p.toLowerCase().includes(needle))
    hits.sort((a, b) => Number(!a.toLowerCase().startsWith(needle)) - Number(!b.toLowerCase().startsWith(needle)) || a.length - b.length)
    return { files: hits.slice(0, limit) }
  },
  'mcp.list'() {
    return { servers: MCP }
  },
  shutdown() {
    setTimeout(() => process.exit(0), 20)
    return {}
  }
}

seed()
out({ event: 'ready', version: VERSION })
logErr(`ready in ${CWD} (model ${model}, speed ${SPEED}x)`)

readline.createInterface({ input: process.stdin }).on('line', (line) => {
  if (!line.trim()) return
  let msg
  try {
    msg = JSON.parse(line)
  } catch {
    return logErr('bad json', line)
  }
  const handler = handlers[msg.method]
  if (!handler) return out({ id: msg.id, error: { message: `Unknown method ${msg.method}` } })
  try {
    out({ id: msg.id, result: handler(msg.params ?? {}) ?? {} })
  } catch (e) {
    out({ id: msg.id, error: { message: e.message } })
  }
}).on('close', () => process.exit(0))
