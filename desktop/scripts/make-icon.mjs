#!/usr/bin/env node
// resources/icon.svg -> build/icon.png (1024, transparent) -> build/icon.icns
// Electron renders the SVG (keeps alpha), sips resizes, iconutil packs the .icns.
import { execFileSync } from 'node:child_process'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const svg = path.join(root, 'resources', 'icon.svg')
const out = path.join(root, 'build')
const png = path.join(out, 'icon.png')
fs.mkdirSync(out, { recursive: true })

const electron = path.join(root, 'node_modules', '.bin', 'electron')
const env = { ...process.env }
delete env.ELECTRON_RUN_AS_NODE
execFileSync(electron, [path.join(root, 'scripts', 'render-icon.cjs'), svg, png], { stdio: 'inherit', env })

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'nanocode-icon-'))
const iconset = path.join(tmp, 'icon.iconset')
fs.mkdirSync(iconset)
for (const size of [16, 32, 128, 256, 512]) {
  for (const scale of [1, 2]) {
    const px = size * scale
    const name = `icon_${size}x${size}${scale === 2 ? '@2x' : ''}.png`
    execFileSync('sips', ['-z', String(px), String(px), png, '--out', path.join(iconset, name)], { stdio: 'ignore' })
  }
}
execFileSync('iconutil', ['-c', 'icns', iconset, '-o', path.join(out, 'icon.icns')])
fs.rmSync(tmp, { recursive: true, force: true })
console.log('wrote build/icon.png and build/icon.icns')
