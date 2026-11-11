// Browser dev mode: the same renderer, served without Electron. It talks to the engine through
// dev/ws-bridge.mjs (see README). `npm run web` starts both.
import { resolve } from 'node:path'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  root: resolve(__dirname, 'src/renderer'),
  resolve: { alias: { '@shared': resolve(__dirname, 'src/shared') } },
  server: { port: 5173, strictPort: true, host: '127.0.0.1' },
  plugins: [react()]
})
