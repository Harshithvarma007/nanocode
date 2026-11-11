#!/usr/bin/env node
// Browser dev mode: lets the renderer run in a normal browser (no Electron). Each WebSocket
// connection spawns one engine and relays newline-delimited JSON both ways.
//
//   node dev/ws-bridge.mjs [--mock] [--port 8787]
//   then open  http://localhost:5173/?bridge=ws://localhost:8787&cwd=/path/to/project
//
// Query parameters on the WebSocket URL (the renderer passes them through):
//   cwd=<dir>          project folder (required)
//   engine=mock|real   override --mock for this connection
//   model=<spec>       passed as --model
// Control messages (not part of the engine protocol) are JSON objects with a "$bridge" key:
//   client → bridge  {"$bridge":"restart","env":{...}}    restart the engine with extra env vars
//   bridge → client  {"$bridge":"state", ...EngineState}   starting / running / exited / not-found
import { spawn } from 'node:child_process'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { WebSocketServer } from 'ws'

const here = path.dirname(fileURLToPath(import.meta.url))
const desktop = path.resolve(here, '..')
const argv = process.argv.slice(2)
const port = Number(argv[argv.indexOf('--port') + 1] || 0) || 8787
const defaultMock = argv.includes('--mock')

// Same as the desktop app (src/main/engine.ts): if there's python3 but no python on PATH, append a
// `python` shim so the agent's `python -m unittest` works.
function shellPath() {
  const dirs = (process.env.PATH ?? '').split(':').filter(Boolean)
  const has = (name) => dirs.some((d) => {
    try {
      fs.accessSync(path.join(d, name), fs.constants.X_OK)
      return true
    } catch {
      return false
    }
  })
  if (has('python') || !has('python3')) return dirs.join(':')
  const shim = path.join(here, '.shims')
  fs.mkdirSync(shim, { recursive: true })
  fs.writeFileSync(path.join(shim, 'python'), '#!/bin/sh\nexec python3 "$@"\n', { mode: 0o755 })
  return [...dirs, shim].join(':')
}

function engineCommand(useMock) {
  if (useMock) return { command: process.execPath, args: [path.join(here, 'mock-engine.mjs')] }
  const venvPython = path.resolve(desktop, '..', '.venv', 'bin', 'python')
  if (process.env.NANOCODE_ENGINE) return { command: process.env.NANOCODE_ENGINE, args: [] }
  if (fs.existsSync(venvPython)) return { command: venvPython, args: ['-m', 'nanocode'] }
  return { command: 'nanocode', args: [] }
}

export function startBridge({ port: listenPort = port, mock = defaultMock } = {}) {
  const wss = new WebSocketServer({ port: listenPort, host: '127.0.0.1' })
  wss.on('connection', (ws, req) => {
    const url = new URL(req.url ?? '/', 'http://localhost')
    const cwd = url.searchParams.get('cwd')
    const useMock = url.searchParams.get('engine') ? url.searchParams.get('engine') === 'mock' : mock
    const model = url.searchParams.get('model')
    let child = null
    let extraEnv = {}
    let expected = false
    const send = (obj) => ws.readyState === ws.OPEN && ws.send(JSON.stringify(obj))

    const start = () => {
      if (!cwd || !fs.existsSync(cwd)) {
        send({ $bridge: 'state', state: 'spawn-error', message: `Project folder not found: ${cwd}`, command: '' })
        return
      }
      const { command, args } = engineCommand(useMock)
      const fullArgs = [...args, 'serve', '--cwd', cwd, ...(model ? ['--model', model] : [])]
      const display = [command, ...fullArgs].join(' ')
      console.log(`[bridge] spawn: ${display}`)
      send({ $bridge: 'state', state: 'starting', command: display })
      const stderrTail = []
      expected = false
      const proc = spawn(command, fullArgs, { cwd, env: { ...process.env, PATH: shellPath(), PYTHONUNBUFFERED: '1', ...extraEnv }, stdio: ['pipe', 'pipe', 'pipe'] })
      child = proc
      proc.on('spawn', () => send({ $bridge: 'state', state: 'running', command: display, pid: proc.pid }))
      proc.on('error', (err) => {
        if (err.code === 'ENOENT') send({ $bridge: 'state', state: 'not-found', searched: [command] })
        else send({ $bridge: 'state', state: 'spawn-error', message: err.message, command: display })
      })
      let buffer = ''
      proc.stdout.setEncoding('utf8')
      proc.stdout.on('data', (chunk) => {
        buffer += chunk
        let i
        while ((i = buffer.indexOf('\n')) >= 0) {
          const line = buffer.slice(0, i).trim()
          buffer = buffer.slice(i + 1)
          if (line && ws.readyState === ws.OPEN) ws.send(line)
        }
      })
      proc.stderr.setEncoding('utf8')
      proc.stderr.on('data', (chunk) => {
        process.stderr.write(`[engine ${proc.pid}] ${chunk}`)
        for (const line of chunk.split('\n')) if (line.trim()) stderrTail.push(line)
        while (stderrTail.length > 60) stderrTail.shift()
      })
      proc.stdin.on('error', () => {})
      proc.on('exit', (code, signal) => {
        console.log(`[bridge] engine ${proc.pid} exited (${code ?? signal})`)
        if (child === proc) child = null
        if (!expected) send({ $bridge: 'state', state: 'exited', code, signal, stderr: stderrTail, command: display })
      })
    }

    const stop = () =>
      new Promise((resolve) => {
        const proc = child
        if (!proc) return resolve()
        expected = true
        proc.once('exit', () => resolve())
        proc.stdin.write(JSON.stringify({ id: 999999999, method: 'shutdown', params: {} }) + '\n')
        setTimeout(() => proc.kill('SIGTERM'), 2500)
      })

    ws.on('message', async (data) => {
      const text = data.toString()
      let msg
      try {
        msg = JSON.parse(text)
      } catch {
        return
      }
      if (msg && msg.$bridge === 'restart') {
        extraEnv = msg.env ?? {}
        send({ $bridge: 'state', state: 'restarting', reason: msg.reason ?? 'Restarting' })
        await stop()
        start()
        return
      }
      child?.stdin.write(text + '\n')
    })
    ws.on('close', () => void stop())
    start()
  })
  console.log(`[bridge] ws://localhost:${listenPort} (${mock ? 'mock engine' : 'real engine'})`)
  return wss
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) startBridge()
