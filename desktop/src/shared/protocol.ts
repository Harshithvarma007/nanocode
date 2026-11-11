// Types for the nanocode engine protocol (v1). Source of truth: ../../../docs/PROTOCOL.md

export type Mode = 'default' | 'accept-edits' | 'plan' | 'yolo'

export interface SessionInfo {
  id: string
  title: string
  created: number
  updated: number
  model: string
  turns: number
}

export interface Todo {
  content: string
  status: 'pending' | 'in_progress' | 'completed'
}

export interface Checkpoint {
  turn_id: string
  prompt: string
  created: number
  files: string[]
}

export interface Status {
  mode: Mode
  model: string
  busy: boolean
  context_used: number
  context_limit: number
}

export interface McpServer {
  name: string
  status: 'connected' | 'failed' | 'disabled'
  tools: string[]
  error?: string
}

export interface CommandInfo {
  name: string
  description: string
}

export interface ModelOption {
  spec: string
  label: string
  detail: string
  group: string
  available: boolean
  needs?: string
}

export interface InitializeResult {
  version: string
  cwd: string
  status: Status
  effort: string
  think: boolean
  session: SessionInfo
  sessions: SessionInfo[]
  commands: CommandInfo[]
  mcp: McpServer[]
  keys: Record<string, boolean>
}

export interface ContextInfo {
  used: number
  limit: number
  window: number
  system: number
  tools: number
  messages: number
  microcompact_at: number
  compact_at: number
}

export interface CostRow {
  model: string
  input: number
  cached: number
  output: number
  cost: string
}

export type DetailKind = '' | 'diff' | 'text' | 'todos'
export type PreviewKind = 'diff' | 'command' | 'url' | 'json'

/** Every event the engine can emit (lines without an `id`). */
export type EngineEvent =
  | { event: 'ready'; version: string }
  | ({ event: 'status' } & Status)
  | { event: 'user'; turn_id: string; text: string }
  | { event: 'stream.start'; parent?: string }
  | { event: 'thinking'; delta: string; parent?: string }
  | { event: 'text'; delta: string; parent?: string }
  | { event: 'tool.preparing'; name: string; parent?: string }
  | { event: 'assistant'; text: string; thinking: string; thinking_seconds: number; parent?: string }
  | {
      event: 'tool.call'
      call_id: string
      name: string
      label: string
      arg: string
      args: Record<string, unknown>
      parent?: string
    }
  | {
      event: 'permission.request'
      request_id: string
      call_id: string
      name: string
      title: string
      question: string
      preview: string
      preview_kind: PreviewKind
      always_label: string
    }
  | { event: 'plan.request'; request_id: string; plan: string }
  | {
      event: 'tool.result'
      call_id: string
      name: string
      summary: string
      detail: string
      detail_kind: DetailKind
      is_error: boolean
      parent?: string
    }
  | { event: 'todos'; todos: Todo[] }
  | { event: 'info' | 'warn' | 'error'; message: string }
  | { event: 'compacted'; before: number; after: number; limit: number; summary: string }
  | { event: 'interrupted' }
  | { event: 'turn.end'; turn_id: string; checkpoint: Checkpoint | null }
  | { event: 'session'; session: SessionInfo }

export interface RpcResponse {
  id: number
  result?: unknown
  error?: { message: string }
}

export type EngineLine = EngineEvent | RpcResponse
