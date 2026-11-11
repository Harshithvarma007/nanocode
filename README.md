# nanocode

**A Claude Code-style coding agent you can read in an afternoon.** It's built from scratch in plain Python
(no agent frameworks) and runs on Claude, any OpenAI-compatible API, or **free local models through Ollama**.

```
╭──────────────────────────────────────────────────────────────╮
│  ✻ Welcome to nanocode!                                       │
│                                                               │
│  /help for help · /model to switch models · shift+tab modes   │
╰──────────────────────────────────────────────────────────────╯
❯ the tests in demo/shop are failing - find out why and fix them

● Read(demo/shop/test_cart.py)
  ⎿  Read 41 lines
● Update(demo/shop/cart.py)
  ⎿  Updated demo/shop/cart.py with 1 addition and 1 removal
         22 -         return sum(item["price"] for item in self.items.values())
         22 +         return sum(item["price"] * item["qty"] for item in self.items.values())
```

This is the companion repo for the video *"Build your own Claude Code from scratch"*. The `stages/` folder
is the build-up from the video, `nanocode/` is the finished agent, and `desktop/` is a Mac app on top of it.

## Features

- **The agent loop:** streaming responses, parallel tool calls, retries, and ctrl+c to interrupt at any point.
- **Ten tools:** `read_file`, `write_file`, `edit_file` (exact-match edits with read-before-edit and
  stale-file checks), `bash`, `glob`, `grep` (ripgrep if installed), `web_fetch`, `task` (subagents),
  `todo_write` and `exit_plan_mode`, plus any tools from your **MCP servers**.
- **Subagents:** `task` sends a read-only helper with its own fresh context to research and report back,
  so the main conversation stays focused.
- **Checkpoints and rewind:** every turn that changes files saves the originals first. `/rewind` puts the
  code back (and, if you like, the conversation).
- **Sessions:** every conversation is saved. `nanocode --continue` picks up the last one; `/resume` lists them.
- **MCP:** a from-scratch MCP client (stdio, JSON-RPC 2.0). Configure servers in `.mcp.json`, the same
  format Claude Code uses.
- **Permissions and modes:** `default` / `accept-edits` / `plan` / `yolo`, switched with **shift+tab**.
  The prompt offers *Yes / Yes, don't ask again / No, and tell it what to do instead*.
- **Plan mode:** the agent researches read-only, proposes a plan, and you approve it before anything changes.
- **Any model:** Claude (official SDK), OpenAI, OpenRouter, Gemini, Groq, LM Studio, or Ollama. You can
  switch models mid-conversation with `/model`.
- **Context engineering:** tool output is capped at the source, old tool results are cleared at 60%, the
  conversation is auto-compacted at 85%, and the system prompt is frozen so prompt caching works. `/context`
  shows how full the window is.
- **Project memory:** `AGENTS.md` (or `CLAUDE.md`) is loaded every session. `/init` writes one, and
  `# note` adds to it.
- **A Claude Code-like interface:** markdown rendering, colored diffs, a todo list, a status bar,
  `@file` mentions, `!shell` commands, slash commands, and custom commands in `.nanocode/commands/*.md`.
- **A desktop app:** `desktop/` is an Electron + React Mac app that drives the same engine through
  `nanocode serve` (see [docs/PROTOCOL.md](docs/PROTOCOL.md)).
- **Tested:** 89 tests, including the real Anthropic and OpenAI SDKs against a local mock server, a real
  MCP server over stdio, and the desktop protocol end to end.

## Quickstart

```bash
git clone https://github.com/Harshithvarma007/nanocode && cd nanocode
uv tool install -e .          # or: python -m venv .venv && .venv/bin/pip install -e .
```

Then pick a model:

**Free and local (Ollama).** Install [Ollama](https://ollama.com), then:
```bash
ollama pull qwen3:8b
nanocode
```

**Claude.** Put `ANTHROPIC_API_KEY=...` in your shell or in a `.env` file (see `.env.example`):
```bash
nanocode --model anthropic:claude-sonnet-5-5
```

**Anything OpenAI-compatible:**
```bash
OPENAI_API_KEY=... nanocode --model openai:<model>
OPENROUTER_API_KEY=... nanocode --model openrouter:<vendor/model>
```

With no `--model`, nanocode picks Claude if `ANTHROPIC_API_KEY` is set, then OpenAI, then the best installed
Ollama model.

## Using it

| Input | What it does |
|---|---|
| `/help` | all commands and shortcuts |
| `/model` | pick a model (installed Ollama models are listed automatically) |
| `/context` | see how full the context window is |
| `/compact [focus]` | summarize the conversation to free up space |
| `/init` | analyze the project and write `AGENTS.md` |
| `/cost` | tokens used and estimated $ (Claude) |
| `/rewind` | undo the file changes of a turn (optionally the conversation since, too) |
| `/resume` | reopen a saved conversation (`nanocode --continue` resumes the latest) |
| `/mode`, `/effort`, `/think`, `/clear`, `/todos`, `/memory`, `/status` | the rest |
| `shift+tab` | cycle modes: default → accept edits → plan |
| `!pytest -q` | run a command yourself; the output is shared with the agent |
| `# always use uv` | save a note to `AGENTS.md` |
| `@src/app.py` | attach a file (tab-completes) |
| `ctrl+c` | interrupt the agent; press it twice at the prompt to quit |

Non-interactive: `nanocode -p "explain this repo"` prints the answer and exits. That's handy in scripts.

**CLI flags:** `--model`, `--mode`, `--yolo`, `--effort`, `--num-ctx`, `--context-limit`, `--no-think`,
`--continue`, `--no-mcp`, `-C <dir>`. `nanocode serve` runs the engine for the desktop app.

## The desktop app

`desktop/` is a Mac app for the same engine: sessions in a sidebar, streaming answers, tool cards with
diffs, inline permission and plan approval, the mode pill (Shift+Tab), a model picker, and an inspector
with todos, checkpoints you can rewind, context usage, MCP servers and cost.

```bash
./packaging/build-engine.sh                 # standalone engine (PyInstaller), bundled into the app
cd desktop && npm install && npm run dist   # -> desktop/release/mac-arm64/nanocode.app (+ a .dmg)
```

It's unsigned, so the first launch needs right-click → Open. For development, `npm run dev` runs it with
the repo's `.venv` engine. Details, the browser dev mode and the mock engine are in
[desktop/README.md](desktop/README.md).

## Local models: what to know

- Use a model with **tool calling**. `/model` only lists the ones that have it. `qwen3:8b` is a good default
  on a 16 GB Mac. Bigger coder models (qwen3-coder, gpt-oss, devstral) do better if you have the memory.
- nanocode uses Ollama's **native** API so it can set `num_ctx` (32k by default, `--num-ctx` to change).
  When a conversation outgrows the window, Ollama silently drops the oldest tokens, which includes the
  system prompt. The agent doesn't crash; it just "forgets" how to be an agent.
- `--no-think` makes thinking models much faster, at some cost to quality.

## How it works

```
nanocode/
  cli.py           entry point, REPL, print mode, `serve`
  agent.py         the loop: call model → run tools → repeat · permissions · interrupts · checkpoints · subagents
  tools.py         the tools, argument validation, output truncation
  permissions.py   modes, safe read-only commands, "don't ask again" rules
  context.py       token estimates, trimming old tool results, compaction
  prompts.py       system prompt, plan-mode reminders, compaction and subagent prompts, AGENTS.md loading
  providers/       one internal message format, three wire formats
    anthropic_provider.py   Claude via the official SDK (prompt caching, thinking, fallbacks)
    openai_provider.py      any OpenAI-compatible API (stitches streamed tool-call fragments)
    ollama_provider.py      Ollama's native /api/chat (sets num_ctx)
  mcp.py           a from-scratch MCP client (stdio JSON-RPC)
  sessions.py      saved conversations, checkpoints, transcripts
  webfetch.py      web_fetch: HTML → readable text
  server.py        `nanocode serve`: the engine behind the desktop app (docs/PROTOCOL.md)
  ui.py            rich + prompt_toolkit: streaming markdown, diffs, menus, status bar
  commands.py      slash commands, !shell, #memory, @mentions, /resume, /rewind
desktop/           the Mac app (Electron + React), a client of `nanocode serve`
packaging/         builds a standalone engine (PyInstaller) that ships inside the app
```

The whole idea fits in five lines:

```python
while True:
    reply = model(system, messages, tools)
    if not reply.tool_calls:
        break
    messages += run(reply.tool_calls)
```

Everything else makes that loop safe (permissions, validation), affordable (context engineering, caching),
and pleasant to use (the UI).

**Learning path:** `stages/01_chat.py` (26 lines) → `stages/02_agent_loop.py` (one tool + a loop) →
`stages/03_mini_agent.py` (a mini Claude Code in 135 lines) → `nanocode/`.

## Configuration

Settings come from CLI flags, then environment variables, then `~/.nanocode/config.json`. You can add your
own OpenAI-compatible provider there:

```json
{
  "model": "ollama:qwen3:8b",
  "num_ctx": 32768,
  "providers": {
    "together": {"kind": "openai", "base_url": "https://api.together.xyz/v1", "api_key_env": "TOGETHER_API_KEY"}
  }
}
```

Environment variables: `NANOCODE_MODEL`, `NANOCODE_MODE`, `NANOCODE_EFFORT`, `NANOCODE_NUM_CTX`,
`NANOCODE_CONTEXT_LIMIT`, `NANOCODE_THINK=0`, `OLLAMA_HOST`, `OPENAI_BASE_URL`.

**MCP servers** go in `.mcp.json` (project), `.nanocode/mcp.json` or `~/.nanocode/mcp.json`:

```json
{
  "mcpServers": {
    "github": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"],
               "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "..."}}
  }
}
```

Their tools show up as `mcp__<server>__<tool>` and always ask before running (until you say "don't ask again").

## Safety

nanocode runs real shell commands and edits real files. In `default` mode it asks before every edit and
every command that isn't a plain read-only one. Commands with pipes, `;`, `&&`, redirects or `$(...)` always
ask. `--yolo` turns all of that off, so only use it in a throwaway directory or a container.

## What it doesn't do (yet)

Claude Code and OpenCode are much bigger products, built by full teams. Things nanocode leaves out: LSP
diagnostics and editor integration, a sandbox for shell commands, hooks and plugins, web search, images,
running independent tools in parallel, git-aware undo for shell side effects, and MCP over HTTP (stdio only).

## Development

```bash
pip install -e '.[dev]' && pytest -q
./demo/reset.sh                         # restore the demo project before recording
./packaging/build-engine.sh             # standalone engine for the desktop app
```

## Credits

nanocode's desktop architecture (one agent server driven by thin clients over a protocol, sessions with
checkpoints, a separate desktop shell) takes its cue from [OpenCode](https://github.com/anomalyco/opencode)
(MIT, Copyright (c) 2025 opencode). nanocode is written from scratch and copies no OpenCode code. It's an
independent learning project, not affiliated with Anthropic or OpenCode.

MIT licensed.
