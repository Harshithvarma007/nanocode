// A tiny logger: userData/logs/main.log (and stdout in dev).
import { app } from 'electron'
import fs from 'node:fs'
import path from 'node:path'

let stream: fs.WriteStream | null = null

export function logsDir(): string {
  return path.join(app.getPath('userData'), 'logs')
}

export function log(message: string): void {
  const line = `${new Date().toISOString()} ${message}\n`
  if (!app.isPackaged) process.stdout.write(`[main] ${line}`)
  try {
    if (!stream) {
      fs.mkdirSync(logsDir(), { recursive: true })
      stream = fs.createWriteStream(path.join(logsDir(), 'main.log'), { flags: 'a' })
    }
    stream.write(line)
  } catch {
    // logging must never crash the app
  }
}
