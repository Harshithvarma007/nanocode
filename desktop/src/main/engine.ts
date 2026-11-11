// Finding and running the nanocode engine (`nanocode serve`), one process per project window.
//
// macOS GUI apps don't inherit the shell's PATH, so we look in this order:
//   1. the engine path set in Settings
//   2. an engine bundled inside the app:  <nanocode.app>/Contents/Resources/engine/
//        engine/nanocode-engine/nanocode-engine   the standalone PyInstaller build that `npm run dist`
//                                                 copies from ../packaging/dist/nanocode-engine
//        engine/bin/nanocode                      or any executable that accepts `serve --cwd <dir>`
//        engine/python/bin/python3                or a relocatable Python with nanocode installed
//   3. in development: ../.venv/bin/python -m nanocode, relative to desktop/
//   4. `nanocode` in ~/.local/bin, /opt/homebrew/bin, /usr/local/bin, or on the login shell's PATH
import { app } from 'electron'
import { execFile, spawn, type ChildProcess } from 'node:child_process'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import type { AppSettings, EngineState } from '../shared/host'
import { log, logsDir } from './log'

export interface EngineCommand {
  command: string
  args: string[]
  source: 'settings' | 'bundled' | 'dev' | 'path'
}

export function describe(cmd: EngineCommand): string {
  return [cmd.command, ...cmd.args].join(' ')
}

function isExecutable(p: string): boolean {
  try {
    fs.accessSync(p, fs.constants.X_OK)
    return fs.statSync(p).isFile()
  } catch {
    return false
  }
}

const PYTHON = /^python(\d+(\.\d+)?)?$/

function fromUserPath(p: string): EngineCommand | null {
  const expanded = p.startsWith('~') ? path.join(os.homedir(), p.slice(1)) : p
  try {
    if (fs.statSync(expanded).isDirectory()) {
      // A nanocode checkout (or a venv): prefer its virtualenv's python.
      for (const py of ['.venv/bin/python', 'bin/python']) {
        const candidate = path.join(expanded, py)
        if (isExecutable(candidate)) return { command: candidate, args: ['-m', 'nanocode'], source: 'settings' }
      }
      const bin = path.join(expanded, 'bin', 'nanocode')
      if (isExecutable(bin)) return { command: bin, args: [], source: 'settings' }
      return null
    }
  } catch {
    return null
  }
  if (!isExecutable(expanded)) return null
  if (PYTHON.test(path.basename(expanded))) return { command: expanded, args: ['-m', 'nanocode'], source: 'settings' }
  return { command: expanded, args: [], source: 'settings' }
}

let shellPathPromise: Promise<string | null> | null = null

/** The PATH a login shell would have (cached). GUI apps start with a minimal PATH. */
export function loginShellPath(): Promise<string | null> {
  if (!shellPathPromise) {
    shellPathPromise = new Promise((resolve) => {
      const shell = process.env.SHELL || '/bin/zsh'
      const marker = '__NANOCODE_PATH__'
      execFile(
        shell,
        ['-ilc', `printf '${marker}%s${marker}' "$PATH"`],
        { timeout: 5000, env: { ...process.env, DISABLE_AUTO_UPDATE: 'true' } },
        (err, stdout) => {
          const match = String(stdout ?? '').match(new RegExp(`${marker}(.*)${marker}`))
          if (match) resolve(match[1])
          else {
            if (err) log(`login shell PATH lookup failed: ${err.message}`)
            resolve(null)
          }
        }
      )
    })
  }
  return shellPathPromise
}

const COMMON_DIRS = [path.join(os.homedir(), '.local', 'bin'), '/opt/homebrew/bin', '/usr/local/bin']

/** PATH for the engine (and the shell commands it runs): login shell PATH + common dirs. */
export async function enginePath(): Promise<string> {
  const parts = [(await loginShellPath()) ?? '', process.env.PATH ?? '', ...COMMON_DIRS].join(':').split(':')
  const dirs = [...new Set(parts.filter(Boolean))]
  const shim = pythonShim(dirs)
  if (shim) dirs.push(shim)
  return dirs.join(':')
}

/** Recent macOS has `python3` but no `python`, and models love to run `python -m …`. If that's the
 *  case, add (at the very end of PATH, so it never shadows a real one) a `python` that runs python3. */
function pythonShim(dirs: string[]): string | null {
  if (dirs.some((d) => isExecutable(path.join(d, 'python')))) return null
  if (!dirs.some((d) => isExecutable(path.join(d, 'python3')))) return null
  const dir = path.join(app.getPath('userData'), 'shims')
  const file = path.join(dir, 'python')
  try {
    fs.mkdirSync(dir, { recursive: true })
    fs.writeFileSync(file, '#!/bin/sh\n# Added by nanocode desktop: this Mac has python3 but no python.\nexec python3 "$@"\n', { mode: 0o755 })
    return dir
  } catch (err) {
    log(`could not create the python shim: ${String(err)}`)
    return null
  }
}

export async function discoverEngine(settings: AppSettings): Promise<{ found: EngineCommand | null; searched: string[] }> {
  const searched: string[] = []

  if (settings.enginePath.trim()) {
    const cmd = fromUserPath(settings.enginePath.trim())
    if (cmd) return { found: cmd, searched }
    searched.push(`${settings.enginePath} (set in Settings — not found or not executable)`)
  }

  // A bundled engine (see the header comment and README.md, "Bundled engine").
  const bundled = path.join(process.resourcesPath ?? '', 'engine')
  const frozen = path.join(bundled, 'nanocode-engine', 'nanocode-engine')
  const bundledBin = path.join(bundled, 'bin', 'nanocode')
  const bundledPython = path.join(bundled, 'python', 'bin', 'python3')
  if (isExecutable(frozen)) return { found: { command: frozen, args: [], source: 'bundled' }, searched }
  if (isExecutable(bundledBin)) return { found: { command: bundledBin, args: [], source: 'bundled' }, searched }
  if (isExecutable(bundledPython)) return { found: { command: bundledPython, args: ['-m', 'nanocode'], source: 'bundled' }, searched }
  if (app.isPackaged) searched.push(`${bundled} (bundled engine)`)

  if (!app.isPackaged) {
    const devPython = path.resolve(app.getAppPath(), '..', '.venv', 'bin', 'python')
    if (isExecutable(devPython)) return { found: { command: devPython, args: ['-m', 'nanocode'], source: 'dev' }, searched }
    searched.push(`${devPython} (development checkout)`)
  }

  for (const dir of COMMON_DIRS) {
    const candidate = path.join(dir, 'nanocode')
    if (isExecutable(candidate)) return { found: { command: candidate, args: [], source: 'path' }, searched }
    searched.push(candidate)
  }

  const shellPath = await loginShellPath()
  for (const dir of (shellPath ?? '').split(':').filter(Boolean)) {
    const candidate = path.join(dir, 'nanocode')
    if (isExecutable(candidate)) return { found: { command: candidate, args: [], source: 'path' }, searched }
  }
  searched.push(`nanocode on your login shell's PATH (${process.env.SHELL || '/bin/zsh'} -lc 'command -v nanocode')`)
  return { found: null, searched }
}

// ------------------------------------------------------------------ the process

export interface EngineOptions {
  cwd: string
  cmd: EngineCommand
  env: NodeJS.ProcessEnv
  model?: string
  onMessage: (message: unknown) => void
  onState: (state: EngineState) => void
}

let engineLog: fs.WriteStream | null = null

function writeEngineLog(line: string): void {
  try {
    if (!engineLog) {
      fs.mkdirSync(logsDir(), { recursive: true })
      engineLog = fs.createWriteStream(path.join(logsDir(), 'engine.log'), { flags: 'a' })
    }
    engineLog.write(line.endsWith('\n') ? line : line + '\n')
  } catch {
    // ignore
  }
}

export class EngineProcess {
  private child: ChildProcess | null = null
  private buffer = ''
  private stderrTail: string[] = []
  private expectedExit = false
  private exited: Promise<void> = Promise.resolve()
  readonly display: string

  constructor(private opts: EngineOptions) {
    this.display = describe(opts.cmd)
  }

  get running(): boolean {
    return this.child !== null
  }

  start(): void {
    const { cmd, cwd, env, model } = this.opts
    const args = [...cmd.args, 'serve', '--cwd', cwd, ...(model ? ['--model', model] : [])]
    const name = path.basename(cwd)
    log(`spawning engine for ${cwd}: ${cmd.command} ${args.join(' ')} (source: ${cmd.source})`)
    this.opts.onState({ state: 'starting', command: this.display })
    let child: ChildProcess
    try {
      child = spawn(cmd.command, args, { cwd, env, stdio: ['pipe', 'pipe', 'pipe'] })
    } catch (err) {
      this.opts.onState({ state: 'spawn-error', message: String(err), command: this.display })
      return
    }
    this.child = child
    this.exited = new Promise((resolve) => child.once('exit', () => resolve()))

    child.on('spawn', () => {
      log(`engine started (pid ${child.pid}) for ${name}`)
      this.opts.onState({ state: 'running', command: this.display, pid: child.pid ?? 0 })
    })
    child.on('error', (err) => {
      log(`engine spawn error: ${err.message}`)
      this.child = null
      this.opts.onState({ state: 'spawn-error', message: err.message, command: this.display })
    })
    child.stdout?.setEncoding('utf8')
    child.stdout?.on('data', (chunk: string) => this.onStdout(chunk))
    child.stderr?.setEncoding('utf8')
    child.stderr?.on('data', (chunk: string) => {
      writeEngineLog(`[${name} ${child.pid}] ${chunk}`)
      for (const line of chunk.split('\n')) {
        if (!line.trim()) continue
        this.stderrTail.push(line)
        if (this.stderrTail.length > 60) this.stderrTail.shift()
      }
    })
    child.stdin?.on('error', () => {
      // EPIPE after the engine died: the exit handler reports it
    })
    child.on('exit', (code, signal) => {
      log(`engine for ${name} exited (code ${code}, signal ${signal}${this.expectedExit ? ', expected' : ''})`)
      this.child = null
      if (!this.expectedExit) {
        this.opts.onState({ state: 'exited', code, signal, stderr: [...this.stderrTail], command: this.display })
      }
    })
  }

  private onStdout(chunk: string): void {
    this.buffer += chunk
    let newline: number
    while ((newline = this.buffer.indexOf('\n')) >= 0) {
      const line = this.buffer.slice(0, newline).trim()
      this.buffer = this.buffer.slice(newline + 1)
      if (!line) continue
      try {
        this.opts.onMessage(JSON.parse(line))
      } catch {
        writeEngineLog(`[stdout, not JSON] ${line}`)
      }
    }
  }

  send(message: unknown): void {
    if (!this.child?.stdin?.writable) return
    this.child.stdin.write(JSON.stringify(message) + '\n')
  }

  /** Ask the engine to save and exit; escalate to signals if it doesn't. */
  async stop(): Promise<void> {
    const child = this.child
    if (!child) return
    this.expectedExit = true
    this.send({ id: 999_999_999, method: 'shutdown', params: {} })
    const timeout = (ms: number) => new Promise<'timeout'>((r) => setTimeout(() => r('timeout'), ms))
    if ((await Promise.race([this.exited, timeout(2500)])) === 'timeout') {
      child.kill('SIGTERM')
      if ((await Promise.race([this.exited, timeout(1500)])) === 'timeout') child.kill('SIGKILL')
    }
  }
}
