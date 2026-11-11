# nanocode desktop

A macOS app for the [nanocode](../README.md) coding agent: Electron + React + TypeScript + Vite.
It's a thin client. All agent logic lives in the Python engine (`nanocode serve`), which the app
starts once per project window and talks to over newline-delimited JSON on stdio
([`docs/PROTOCOL.md`](../docs/PROTOCOL.md)).

## Run it

```bash
cd desktop
npm install            # downloads Electron (see "Install notes" if it fails)
npm run dev            # Electron + Vite with hot reload
```

In development the app uses the repo's own engine, `../.venv/bin/python -m nanocode`, so create that
venv first (`python -m venv .venv && .venv/bin/pip install -e .` in the repo root).

To open a folder straight away, pass it to a built app: `npm run build && npx electron . ~/code/shop` (or `open -a nanocode ~/code/shop` once it's installed).

### Browser dev mode (no Electron)

The renderer also runs in a normal browser. A small WebSocket bridge spawns the engine and relays
its lines:

```bash
npm run web -- --cwd ../demo/shop            # real engine (../.venv/bin/python -m nanocode serve)
npm run web:mock -- --cwd ../demo/shop       # the mock engine in dev/mock-engine.mjs
# then open the URL it prints, e.g.
#   http://localhost:5173/?bridge=ws://localhost:8787&cwd=/abs/path/to/shop[&model=ollama:qwen3:8b]
```

`window.nano` (the preload API) is used when present; otherwise the renderer connects to the bridge.
In the browser, settings live in `localStorage` and API keys stay in memory for that tab only.

### The mock engine

`dev/mock-engine.mjs` speaks the full protocol and replays realistic turns: streaming thinking and
text, tool calls with diffs, permission and plan requests, todos, a subagent Task with nested events,
auto-compaction, errors and interrupts. It never writes to disk. Prompts it reacts to: anything (the
"fix the failing tests" turn), `explain …`, `plan …` (or plan mode), `error`, `long` (to test Stop),
`/compact`, `/init`, `!ls`, `# note`. `MOCK_SPEED=3` speeds it up.

## Build the app

```bash
npm run typecheck      # main + preload + renderer
npm run build          # typecheck, then electron-vite build -> out/
npm run dist           # build, check the engine, electron-builder -> release/
npm run icon           # regenerate build/icon.icns from resources/icon.svg
```

`npm run dist` produces an unsigned arm64 **`release/mac-arm64/nanocode.app`** and
`release/nanocode-<version>-arm64.dmg`. Because it isn't signed, the first launch needs right-click →
Open (or `xattr -dr com.apple.quarantine nanocode.app` after downloading it).

`npm run dist` bundles the standalone engine from `../packaging/dist/nanocode-engine/` (a PyInstaller
build, so the app needs no Python). If it's missing, `scripts/check-engine.mjs` runs
`../packaging/build-engine.sh` first, and fails with a clear message if that isn't possible.

## Architecture

```
┌──────────────── nanocode.app ────────────────┐
│  main (src/main)                             │      one per project window
│   windows · menus · settings · safeStorage   │   ┌──────────────────────────┐
│   engine discovery + process ────────────────┼──▶│ nanocode serve --cwd …   │
│        ▲ IPC (contextBridge)                 │   │ (Python, JSON over stdio)│
│  preload (src/preload) → window.nano         │   └──────────────────────────┘
│        ▲                                     │
│  renderer (src/renderer, React)              │
│   lib/rpc.ts      request/response by id      │
│   lib/store.ts    one reducer for live events │
│                   and session.load history    │
│   components/     transcript, composer,       │
│                   inspector, settings …       │
└──────────────────────────────────────────────┘
```

- **main** (`src/main/index.ts`, `engine.ts`, `store.ts`) owns the windows and the engine processes.
  It relays engine stdout lines to its window as `engine:message` and window messages to engine stdin.
  It never interprets the conversation.
- **preload** (`src/preload/index.ts`) exposes `window.nano` (`src/shared/host.ts`): engine I/O, folder
  picker, recents, settings, notifications. The renderer is sandboxed with context isolation.
- **renderer** (`src/renderer/src`) is the whole UI. `lib/store.ts` turns protocol events into
  transcript items; the same reducer replays `session.load` transcripts, so history renders exactly
  like live turns. Streaming deltas are batched (~30 fps).
- **engine**: see `../docs/PROTOCOL.md`. The app sends `initialize` after `ready`, then drives
  everything with requests (`prompt`, `permission.respond`, `checkpoint.restore`, …).

### Finding the engine

GUI apps on macOS don't inherit your shell's `PATH`, so `src/main/engine.ts` looks in this order:

1. the engine path in **Settings → Engine** (a `nanocode` executable, a Python that has nanocode
   installed, or a nanocode checkout folder);
2. a **bundled engine** in the app: `Contents/Resources/engine/nanocode-engine/nanocode-engine` (what
   `npm run dist` ships), or `engine/bin/nanocode`, or `engine/python/bin/python3 -m nanocode`;
3. in development, `../.venv/bin/python -m nanocode` relative to `desktop/`;
4. `nanocode` in `~/.local/bin`, `/opt/homebrew/bin`, `/usr/local/bin`, or on the login shell's `PATH`.

If nothing is found the window shows install instructions, where it looked, a **Choose engine…**
button and **Try again**. If the engine exits, the window shows its last stderr lines and **Restart
engine**. The engine gets the login shell's `PATH` so the commands the agent runs (python, git, rg)
work like they do in Terminal. If the Mac has `python3` but no `python`, a tiny `python` → `python3`
shim is appended to the end of that `PATH` (`userData/shims/`), since models like to run `python -m …`.

### Settings and secrets

Stored in Electron's `userData` (`~/Library/Application Support/nanocode/`):

- `settings.json`: engine path, default model, Ollama host, theme, effort, thinking.
- `keys.json`: API keys for Anthropic, OpenAI, OpenRouter, Gemini and Groq, **encrypted with
  `safeStorage`** (the macOS Keychain). Only the last four characters are stored in clear text, for the
  `•••• 1234` display. Keys are decrypted only when an engine starts and are passed as environment
  variables; saving or removing a key restarts the engine and reopens the current session.
- `recents.json`, and `logs/main.log` + `logs/engine.log` (engine stderr). Help → Open Logs Folder.

### Keyboard

| | |
|---|---|
| ↩ / ⇧↩ | send / new line |
| `/` `@` `!` `#` | commands · attach a file · run a shell command · save to AGENTS.md |
| ⇧⇥ | cycle mode: default → accept edits → plan |
| esc, ⌘. | stop the turn |
| ↩ or 1 · 2 · esc or 3 | answer a permission or plan request |
| ⌘N · ⌘O · ⌘, | new session · open folder · settings |
| ⌘\\ · ⌥⌘I · ⌘L · ⌘/ | sidebar · inspector · focus the message box · shortcuts |

### Test hook

`NANOCODE_SNAPSHOT=/tmp/shot.png` makes each new window capture itself (with its native title bar)
via `screencapture -l` after `NANOCODE_SNAPSHOT_DELAY` ms (default 8000). Useful for checking the
real window from a script.

## Install notes

- npm 11 asks before running install scripts. If Electron's binary didn't download, run
  `npm approve-scripts electron esbuild` and `npm rebuild electron`.
- Node 20+ is required for the dev tooling.

## Credits

The architecture (one agent server, thin clients that drive it) and parts of the desktop UX follow
**[OpenCode](https://github.com/anomalyco/opencode)** (MIT, © 2025 opencode), whose desktop app is
also Electron on top of a separate agent server. No OpenCode code is copied here.

MIT licensed, like the rest of nanocode.
