#!/usr/bin/env node
// Before packaging: make sure the standalone engine (PyInstaller build) exists, building it if we can.
// electron-builder copies it into nanocode.app/Contents/Resources/engine/nanocode-engine/.
import { execFileSync } from 'node:child_process'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const packaging = path.resolve(root, '..', 'packaging')
const exe = path.join(packaging, 'dist', 'nanocode-engine', 'nanocode-engine')
const script = path.join(packaging, 'build-engine.sh')

if (!fs.existsSync(exe)) {
  if (!fs.existsSync(script)) {
    console.error(`\n✗ The standalone engine is missing: ${exe}\n  and there is no ${script} to build it.\n`)
    process.exit(1)
  }
  console.log(`Building the standalone engine with ${path.relative(root, script)} …`)
  try {
    execFileSync('bash', [script], { stdio: 'inherit', cwd: packaging })
  } catch {
    console.error('\n✗ build-engine.sh failed, so the app would ship without an engine. Fix it and run npm run dist again.\n')
    process.exit(1)
  }
  if (!fs.existsSync(exe)) {
    console.error(`\n✗ build-engine.sh finished but ${exe} is still missing.\n`)
    process.exit(1)
  }
}
console.log(`✓ engine: ${path.relative(root, exe)}`)
