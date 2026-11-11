#!/usr/bin/env node
// `npm run web [-- --mock] [-- --cwd <dir>]`: the WebSocket bridge + the renderer's Vite server.
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'
import { startBridge } from './ws-bridge.mjs'

const here = path.dirname(fileURLToPath(import.meta.url))
const argv = process.argv.slice(2)
const mock = argv.includes('--mock')
const cwdArg = argv.includes('--cwd') ? path.resolve(argv[argv.indexOf('--cwd') + 1]) : path.resolve(here, '..', '..', 'demo', 'shop')

startBridge({ port: 8787, mock })
const server = await createServer({ configFile: path.join(here, '..', 'vite.web.config.ts') })
await server.listen()
const url = `http://localhost:${server.config.server.port}/?bridge=${encodeURIComponent('ws://localhost:8787')}&cwd=${encodeURIComponent(cwdArg)}`
console.log(`\n  nanocode (browser dev mode${mock ? ', mock engine' : ''})\n  ${url}\n`)
