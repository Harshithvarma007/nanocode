"""Slash commands and special input.

    /command args   built-in or custom commands (.nanocode/commands/*.md)
    !ls -la         run a shell command yourself; the output goes into the conversation
    # always use uv save a note to project memory (AGENTS.md)
    @src/app.py     attach a file to your message
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from rich.table import Table
from rich.text import Text

from .config import EFFORTS, HOME, MODEL_PRESETS, MODES, ollama_models
from .context import COMPACT_AT, MICROCOMPACT_AT, estimate_tokens, todos_text
from .prompts import INIT_PROMPT
from .providers import ProviderError
from .sessions import SessionStore, new_session_id, restore, snapshot
from .tools import execute
from .ui import ACCENT, fmt_tokens, todo_lines

COMMANDS: dict[str, tuple[str, object]] = {}

# $ per million tokens: input, output, cache read, cache write (5-minute TTL)
PRICES = {
    "claude-opus-5-5": (4.00, 20.00, 0.20, 5.00),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20, 2.50),
    "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125),
    "claude-fable-5-1": (10.00, 50.00, 0.25, 12.50),
    "claude-opus-5": (5.00, 25.00, 0.50, 6.25),
    "claude-sonnet-5": (2.00, 10.00, 0.20, 2.50),
}


def command(name: str, description: str):
    def register(fn):
        COMMANDS[name] = (description, fn)
        return fn

    return register


def custom_commands(agent) -> dict[str, Path]:
    found: dict[str, Path] = {}
    for folder in (HOME / "commands", agent.root / ".nanocode" / "commands"):
        if folder.is_dir():
            for path in sorted(folder.glob("*.md")):
                found[path.stem] = path
    return found


def all_commands(agent) -> list[tuple[str, str]]:
    items = [(name, description) for name, (description, _) in COMMANDS.items()]
    items += [(name, "custom command") for name in custom_commands(agent) if name not in COMMANDS]
    return items


# --------------------------------------------------------------------------- input dispatch

def dispatch(agent, ui, text: str) -> bool:
    """Handle one line of input. Returns False when the user wants to quit."""
    if text.startswith("/"):
        name, _, arg = text[1:].partition(" ")
        if name in COMMANDS:
            return COMMANDS[name][1](agent, ui, arg.strip()) is not False
        custom = custom_commands(agent)
        if name in custom:
            template = custom[name].read_text(encoding="utf-8")
            prompt = template.replace("$ARGUMENTS", arg) if "$ARGUMENTS" in template else (template + "\n\n" + arg).strip()
            agent.run_turn(expand_mentions(agent, ui, prompt))
            return True
        ui.error(f"Unknown command /{name}. Type /help to see what's available.")
        return True
    if text.startswith("!"):
        run_shell(agent, ui, text[1:].strip())
        return True
    if text.startswith("#") and not text.startswith("##"):
        remember(agent, ui, text[1:].strip())
        return True
    agent.run_turn(expand_mentions(agent, ui, text))
    return True


MENTION = re.compile(r"(?<![\w@])@([\w./~-]+)")


def expand_mentions(agent, ui, text: str) -> str:
    """`@path/to/file` attaches the file's contents to the message."""
    attachments = []
    for match in MENTION.finditer(text):
        path = agent.ctx.resolve(match.group(1).rstrip(".,;:"))
        if not path.is_file():
            continue
        result = execute(agent.ctx, "read_file", {"path": str(path)})
        if not result.is_error:
            rel = agent.ctx.rel(path)
            attachments.append(f'<file path="{rel}">\n{result.output}\n</file>')
            ui.info(f"Attached {rel}")
    return text + ("\n\n" + "\n\n".join(attachments) if attachments else "")


def run_shell(agent, ui, command_line: str) -> None:
    if not command_line:
        return
    args = {"command": command_line}
    ui.tool_call("bash", args)
    with ui.tool_spinner("bash", args):
        result = execute(agent.ctx, "bash", args)
    ui.tool_result("bash", args, result)
    agent.messages.append({"role": "user", "content": f"I ran this shell command myself:\n$ {command_line}\n{result.output}"})


def remember(agent, ui, note: str) -> None:
    if not note:
        return
    path = agent.root / "AGENTS.md"
    existing = path.read_text(encoding="utf-8") if path.exists() else "# AGENTS.md\n\nNotes for coding agents working in this project.\n"
    path.write_text(existing.rstrip("\n") + f"\n- {note}\n", encoding="utf-8")
    # The system prompt is frozen for this session, so we also tell the model right now.
    agent.messages.append({"role": "user", "content": f"<system-reminder>The user saved this to project memory "
                                                      f"(AGENTS.md), follow it from now on: {note}</system-reminder>"})
    ui.info("Saved to AGENTS.md - it's loaded automatically in every future session")


# --------------------------------------------------------------------------- built-in commands

@command("help", "Show commands and keyboard shortcuts")
def _help(agent, ui, arg):
    table = Table.grid(padding=(0, 3))
    table.add_column(style=f"bold {ACCENT}", no_wrap=True)
    table.add_column(style="muted")
    for name, description in all_commands(agent):
        table.add_row("/" + name, description)
    ui.console.print()
    ui.console.print(Text("Commands", style="bold"))
    ui.console.print(table)
    shortcuts = Table.grid(padding=(0, 3))
    shortcuts.add_column(style="bold", no_wrap=True)
    shortcuts.add_column(style="muted")
    for keys, what in [("! <command>", "run a shell command yourself (output is shared with the agent)"),
                       ("# <note>", "save a note to project memory (AGENTS.md)"),
                       ("@<path>", "attach a file to your message (tab-completes)"),
                       ("shift+tab", "cycle modes: default → accept edits → plan"),
                       ("ctrl+c", "interrupt the agent · press twice at the prompt to quit"),
                       ("\\ + enter, ctrl+j", "insert a newline")]:
        shortcuts.add_row(keys, what)
    ui.console.print()
    ui.console.print(Text("Shortcuts", style="bold"))
    ui.console.print(shortcuts)


def _model_options(agent) -> list[tuple[str, str]]:
    options = []
    for spec, description in MODEL_PRESETS:
        provider = spec.split(":", 1)[0]
        key_env = agent.settings.providers[provider].get("api_key_env")
        ready = "" if not key_env or os.environ.get(key_env) else f"  (needs {key_env})"
        options.append((spec, f"{spec:<30} {description}{ready}"))
    for m in ollama_models(agent.settings.providers["ollama"]["base_url"]):
        if m["tools"]:
            spec = f"ollama:{m['name']}"
            tags = "tools" + (", thinking" if m["thinking"] else "")
            options.append((spec, f"{spec:<30} local · {m['size']} · {tags} · free"))
    options.append(("custom", "Type any provider:model…"))
    options.append(("cancel", "Cancel"))
    return options


@command("model", "Switch model, e.g. /model ollama:qwen3:8b or /model anthropic:claude-sonnet-5-5")
def _model(agent, ui, arg):
    if not arg:
        ui.console.print(Text(f"Current model: {agent.provider.label}", style="muted"))
        options = _model_options(agent)
        current = agent.provider.label if any(spec == agent.provider.label for spec, _ in options) else None
        arg = ui.choose("Select a model", options, default=current)
        if arg == "cancel":
            return
        if arg == "custom":
            arg = ui.ask_text("  provider:model ❯ ")
            if not arg:
                return
    try:
        with ui.spinner(f"Connecting to {arg}"):
            agent.set_model(arg)
    except ProviderError as e:
        ui.error(str(e))
        return
    ui.info(f"Now using {agent.provider.label} · context budget {fmt_tokens(agent.context_limit())} tokens")


@command("mode", "Show or set the mode: default, accept-edits, plan, yolo")
def _mode(agent, ui, arg):
    if not arg:
        ui.info(f"Mode: {agent.permissions.mode}  (options: {', '.join(MODES)} · shift+tab cycles)")
        return
    if arg not in MODES:
        ui.error(f"Unknown mode '{arg}'. Options: {', '.join(MODES)}")
        return
    if arg == "yolo":
        agent.settings.allow_yolo_cycle = True
    agent.permissions.mode = arg
    ui.info(f"Mode set to {arg}")


@command("effort", "Set Claude's reasoning effort: low, medium, high, xhigh, max")
def _effort(agent, ui, arg):
    if arg not in EFFORTS:
        ui.info(f"Effort: {agent.settings.effort}  (options: {', '.join(EFFORTS)})")
        return
    agent.settings.effort = arg
    if hasattr(agent.provider, "effort"):
        agent.provider.effort = arg
    ui.info(f"Effort set to {arg}" + ("" if agent.provider.kind == "anthropic" else " (applies to Claude models)"))


@command("think", "Turn reasoning on/off for local models, and show/hide thinking: /think on|off")
def _think(agent, ui, arg):
    if arg not in ("on", "off"):
        ui.info(f"Thinking is {'on' if agent.settings.think else 'off'}. Use /think on or /think off.")
        return
    on = arg == "on"
    agent.settings.think = agent.settings.show_thinking = ui.show_thinking = on
    if hasattr(agent.provider, "think"):
        agent.provider.think = on
    ui.info(f"Thinking {arg}")


@command("compact", "Summarize the conversation to free up context: /compact [what to focus on]")
def _compact(agent, ui, arg):
    agent.compact(arg, auto=False)


@command("clear", "Start a fresh conversation")
def _clear(agent, ui, arg):
    save_session(agent)
    agent.reset()
    attach_session(agent)
    ui.info("Started a new conversation (the old one is saved: /resume)")


@command("context", "Visualize how full the context window is")
def _context(agent, ui, arg):
    limit, used = agent.context_limit(), agent.context_used()
    system, tools = estimate_tokens(agent.system), estimate_tokens(json.dumps(agent.tools))
    messages = max(0, used - system - tools)
    width = 50
    bar = Text("  ")
    cells_used = 0
    segments = [("System prompt", system, "#7aa2f7"), ("Tools", tools, "#bb9af7"), ("Messages", messages, ACCENT)]
    for _, n, color in segments:
        cells = max(1 if n else 0, round(width * n / limit))
        bar.append("█" * cells, style=color)
        cells_used += cells
    bar.append("░" * max(0, width - cells_used), style="grey35")
    pct = 100 * used / limit
    ui.console.print()
    ui.console.print(Text.assemble(("  Context usage ", "bold"),
                                   (f"{agent.provider.label} · {fmt_tokens(used)} / {fmt_tokens(limit)} tokens "
                                    f"({pct:.0f}%)", "muted")))
    ui.console.print(bar)
    for label, n, color in segments:
        ui.console.print(Text.assemble(("  █ ", color), (f"{label}: ", ""), (f"{fmt_tokens(n)} tokens", "muted")))
    ui.console.print(Text.assemble(("  ░ ", "grey35"), ("Free: ", ""), (f"{fmt_tokens(max(0, limit - used))} tokens", "muted")))
    ui.console.print(Text(f"  Old tool results are cleared at {MICROCOMPACT_AT:.0%}; the conversation is "
                          f"auto-compacted at {COMPACT_AT:.0%}.", style="muted"))
    if limit < agent.provider.context_window:
        ui.console.print(Text(f"  Budget capped at {fmt_tokens(limit)} of the model's {fmt_tokens(agent.provider.context_window)} "
                              "window (set NANOCODE_CONTEXT_LIMIT to change).", style="muted"))


def _cost(spec: str, u) -> str:
    model = spec.split(":", 1)[1]
    if spec.startswith("ollama:"):
        return "$0.00 (local)"
    if model not in PRICES:
        return "not tracked"
    price_in, price_out, price_read, price_write = PRICES[model]
    uncached = max(0, u.input_tokens - u.cache_read_tokens - u.cache_write_tokens)
    dollars = (uncached * price_in + u.output_tokens * price_out + u.cache_read_tokens * price_read
               + u.cache_write_tokens * price_write) / 1e6
    return f"${dollars:.4f}"


@command("cost", "Show token usage and estimated cost for this session")
def _cost_command(agent, ui, arg):
    if not agent.usage_by_model:
        ui.info("No model calls yet.")
        return
    table = Table(box=None, padding=(0, 2), header_style="bold")
    for column in ("model", "input", "cached", "output", "cost"):
        table.add_column(column, justify="left" if column == "model" else "right")
    for spec, u in agent.usage_by_model.items():
        table.add_row(spec, fmt_tokens(u.input_tokens), fmt_tokens(u.cache_read_tokens), fmt_tokens(u.output_tokens),
                      _cost(spec, u))
    ui.console.print()
    ui.console.print(table)


@command("todos", "Show the current todo list")
def _todos(agent, ui, arg):
    if not agent.ctx.todos:
        ui.info("No todos yet.")
    else:
        ui.console.print(todo_lines(agent.ctx.todos))


@command("init", "Analyze this project and write an AGENTS.md memory file")
def _init(agent, ui, arg):
    agent.run_turn(INIT_PROMPT)


@command("memory", "Show which memory files are loaded")
def _memory(agent, ui, arg):
    if not agent.memory:
        ui.info("No memory files loaded. Run /init to create AGENTS.md, or add a note with: # <note>")
        return
    for path, text in agent.memory:
        ui.info(f"{path}  ({fmt_tokens(estimate_tokens(text))} tokens)")
    ui.info("Edits take effect in the next session (/clear). Add a note now with: # <note>")


@command("status", "Show the current model, mode and settings")
def _status(agent, ui, arg):
    s = agent.settings
    rows = [("model", agent.provider.label), ("mode", agent.permissions.mode), ("effort", s.effort),
            ("thinking", "on" if s.think else "off"),
            ("context", f"{fmt_tokens(agent.context_used())} / {fmt_tokens(agent.context_limit())} tokens"),
            ("cwd", str(agent.root))]
    if agent.ctx.todos:
        rows.append(("todos", todos_text(agent.ctx.todos).replace("\n", " · ")))
    for key, value in rows:
        ui.console.print(Text.assemble((f"  {key:<9}", "muted"), value))


@command("resume", "Resume a saved conversation")
def _resume(agent, ui, arg):
    store = getattr(agent, "store", None) or SessionStore(agent.root)
    sessions = [s for s in store.list() if s["id"] != getattr(agent, "session_id", None)][:20]
    if not sessions:
        ui.info("No other saved sessions for this project.")
        return
    options = [(s["id"], f"{time.strftime('%b %d %H:%M', time.localtime(s['updated']))}  {s['title'][:50]}"
                         f"  ({s['turns']} turns)") for s in sessions] + [("cancel", "Cancel")]
    choice = ui.choose("Resume which session?", options)
    if choice == "cancel":
        return
    save_session(agent)
    data = store.load(choice)
    attach_session(agent, data)
    ui.info(f"Resumed \"{data.get('title') or 'session'}\"")
    last = agent.last_text()
    if last:
        ui.print_assistant(last)


@command("rewind", "Undo the file changes of a turn (and optionally the conversation since)")
def _rewind(agent, ui, arg):
    if not agent.checkpoints:
        ui.info("No file changes to rewind in this session yet.")
        return
    options = [(c["turn_id"], f"{c['prompt'][:60]}  ({len(c['files'])} file{'' if len(c['files']) == 1 else 's'})")
               for c in reversed(agent.checkpoints)] + [("cancel", "Cancel")]
    turn_id = ui.choose("Rewind to before which prompt? (this and every later turn is undone)", options)
    if turn_id == "cancel":
        return
    how = ui.choose("What should be restored?", [("code", "Code only"), ("both", "Code and conversation"),
                                                  ("cancel", "Cancel")])
    if how == "cancel":
        return
    restored = agent.restore_checkpoint(turn_id, conversation=how == "both")
    ui.info(f"Restored {len(restored)} file{'' if len(restored) == 1 else 's'}: {', '.join(restored)}"
            + (" · conversation rewound" if how == "both" else "")
            + ". Changes made by shell commands aren't undone.")


@command("exit", "Quit nanocode")
def _exit(agent, ui, arg):
    return False


COMMANDS["quit"] = ("Quit nanocode", _exit)


# --------------------------------------------------------------------------- sessions (terminal UI)

def attach_session(agent, data: dict | None = None) -> None:
    """Give the agent a session to save into: a new one, or a loaded one."""
    agent.store = getattr(agent, "store", None) or SessionStore(agent.root)
    if data:
        restore(agent, data)
        agent.session_id, agent.session_title = data["id"], data.get("title", "")
        agent.session_created, agent.display = data.get("created", time.time()), data.get("display", [])
    else:
        agent.session_id, agent.session_title = new_session_id(), ""
        agent.session_created, agent.display = time.time(), []


def save_session(agent) -> None:
    if getattr(agent, "store", None) is None or not agent.messages:
        return
    if not agent.session_title:
        first = next((m["content"] for m in agent.messages if m["role"] == "user"
                      and not m["content"].startswith(("<system-reminder>", "["))), "")
        agent.session_title = " ".join(first.split("\n\n<file path=")[0].split())[:60]
    try:
        agent.store.save(snapshot(agent, agent.session_id, agent.session_title, agent.session_created,
                                  getattr(agent, "display", [])))
    except OSError:
        pass
