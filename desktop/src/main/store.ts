// Persistent app state in Electron's userData folder:
//   settings.json   non-secret settings
//   recents.json    recently opened projects
//   keys.json       API keys, encrypted with safeStorage (macOS Keychain). Only the last 4
//                   characters are kept in clear text, for display.
import { app, safeStorage } from 'electron'
import fs from 'node:fs'
import path from 'node:path'
import { KEY_ENVS, type AppSettings, type KeyHint, type Recent, type SettingsState } from '../shared/host'
import { log } from './log'

const DEFAULTS: AppSettings = {
  enginePath: '',
  defaultModel: '',
  ollamaHost: '',
  theme: 'system',
  effort: 'high',
  think: true
}

type StoredKey = { cipher: string; last4: string }

function file(name: string): string {
  return path.join(app.getPath('userData'), name)
}

function readJson<T>(name: string, fallback: T): T {
  try {
    return JSON.parse(fs.readFileSync(file(name), 'utf8')) as T
  } catch {
    return fallback
  }
}

function writeJson(name: string, value: unknown, mode = 0o644): void {
  fs.mkdirSync(app.getPath('userData'), { recursive: true })
  const target = file(name)
  const tmp = `${target}.tmp`
  fs.writeFileSync(tmp, JSON.stringify(value, null, 2), { mode })
  fs.renameSync(tmp, target)
}

export function getSettings(): AppSettings {
  return { ...DEFAULTS, ...readJson<Partial<AppSettings>>('settings.json', {}) }
}

export function setSettings(patch: Partial<AppSettings>): AppSettings {
  const next = { ...getSettings(), ...patch }
  writeJson('settings.json', next)
  return next
}

// ------------------------------------------------------------------ API keys

function storedKeys(): Record<string, StoredKey> {
  return readJson<Record<string, StoredKey>>('keys.json', {})
}

export function keyHints(): Record<string, KeyHint> {
  const stored = storedKeys()
  const out: Record<string, KeyHint> = {}
  for (const { env } of KEY_ENVS) out[env] = { set: Boolean(stored[env]), last4: stored[env]?.last4 ?? '' }
  return out
}

export function secureStorageAvailable(): boolean {
  try {
    return safeStorage.isEncryptionAvailable()
  } catch {
    return false
  }
}

export function setKey(env: string, value: string | null): void {
  if (!KEY_ENVS.some((k) => k.env === env)) throw new Error(`Unknown key ${env}`)
  const stored = storedKeys()
  const clean = value?.trim() ?? ''
  if (!clean) {
    delete stored[env]
  } else {
    if (!secureStorageAvailable()) throw new Error('Secure storage (Keychain) is not available, so the key was not saved.')
    stored[env] = { cipher: safeStorage.encryptString(clean).toString('base64'), last4: clean.slice(-4) }
  }
  writeJson('keys.json', stored, 0o600)
  log(`api key ${env} ${clean ? 'saved' : 'removed'}`)
}

/** Decrypted keys, as env vars for the engine. Only touches the Keychain when keys exist. */
export function keyEnv(): Record<string, string> {
  const stored = storedKeys()
  const out: Record<string, string> = {}
  for (const [env, { cipher }] of Object.entries(stored)) {
    try {
      out[env] = safeStorage.decryptString(Buffer.from(cipher, 'base64'))
    } catch (err) {
      log(`could not decrypt ${env}: ${String(err)}`)
    }
  }
  return out
}

export function settingsState(): SettingsState {
  return { settings: getSettings(), keys: keyHints(), secure: secureStorageAvailable() }
}

// ------------------------------------------------------------------ recent projects

export function recents(): Recent[] {
  return readJson<Recent[]>('recents.json', []).filter((r) => {
    try {
      return fs.statSync(r.path).isDirectory()
    } catch {
      return false
    }
  })
}

export function addRecent(dir: string): Recent[] {
  const list = recents().filter((r) => r.path !== dir)
  list.unshift({ path: dir, name: path.basename(dir), opened: Date.now() })
  const trimmed = list.slice(0, 12)
  writeJson('recents.json', trimmed)
  app.addRecentDocument(dir)
  return trimmed
}

export function removeRecent(dir: string): Recent[] {
  const list = recents().filter((r) => r.path !== dir)
  writeJson('recents.json', list)
  return list
}
