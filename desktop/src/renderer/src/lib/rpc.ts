// Request/response over the engine's newline-delimited JSON (see docs/PROTOCOL.md).
import type { EngineEvent, RpcResponse } from '@shared/protocol'
import { host } from './host'

interface Pending {
  method: string
  resolve: (value: unknown) => void
  reject: (err: Error) => void
}

export class EngineError extends Error {}

class EngineClient {
  private nextId = 1
  private pending = new Map<number, Pending>()
  private eventListeners = new Set<(event: EngineEvent) => void>()

  constructor() {
    host.onEngineMessage((message) => this.handle(message))
  }

  request<T = unknown>(method: string, params: object = {}): Promise<T> {
    const id = this.nextId++
    return new Promise<T>((resolve, reject) => {
      this.pending.set(id, { method, resolve: resolve as (v: unknown) => void, reject })
      host.engineSend({ id, method, params })
    })
  }

  onEvent(cb: (event: EngineEvent) => void): () => void {
    this.eventListeners.add(cb)
    return () => void this.eventListeners.delete(cb)
  }

  /** The engine went away: fail every outstanding request. */
  rejectAll(reason: string): void {
    for (const p of this.pending.values()) p.reject(new EngineError(reason))
    this.pending.clear()
  }

  private handle(message: unknown): void {
    if (!message || typeof message !== 'object') return
    const m = message as Partial<RpcResponse> & { event?: string }
    if (typeof m.event === 'string') {
      for (const cb of this.eventListeners) cb(m as EngineEvent)
      return
    }
    if (typeof m.id !== 'number') return
    const p = this.pending.get(m.id)
    if (!p) return
    this.pending.delete(m.id)
    if (m.error) p.reject(new EngineError(m.error.message || `${p.method} failed`))
    else p.resolve(m.result ?? {})
  }
}

export const engine = new EngineClient()
