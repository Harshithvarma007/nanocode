"""The terminal UI: everything the human sees and types.

Rendering uses `rich` (markdown, colors, live-updating spinners). Input uses `prompt_toolkit`
(history, autocomplete for /commands and @files, shift+tab to switch modes, a status bar).
"""

from __future__ import annotations

import contextlib
import math
import os
import random
import re
import time

from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

from .config import HOME
from .permissions import describe_request
from .tools import ToolResult, walk_files

ACCENT = "#e8925b"
THEME = Theme({"accent": ACCENT, "muted": "grey58", "err": "#ff6b6b", "warn": "#e5c07b", "ok": "#98c379",
               "plan": "#56b6c2", "edits": "#c678dd", "markdown.code": "#e5c07b"})
TOOL_LABELS = {"read_file": "Read", "write_file": "Write", "edit_file": "Update", "bash": "Bash", "glob": "Glob",
               "grep": "Grep", "todo_write": "Update Todos", "exit_plan_mode": "Plan", "task": "Task",
               "web_fetch": "Fetch"}


def tool_label(name: str) -> str:
    if name.startswith("mcp__"):
        _, server, tool = (name.split("__", 2) + ["", ""])[:3]
        return f"{server} · {tool} (MCP)"
    return TOOL_LABELS.get(name, name)
VERBS = ["Thinking", "Pondering", "Noodling", "Percolating", "Cogitating", "Tinkering", "Scheming", "Brewing",
         "Mulling", "Puzzling", "Conjuring", "Crafting", "Wrangling", "Untangling", "Simmering", "Hatching"]
SPINNER = ["·", "✢", "✳", "✶", "✻", "✽", "✻", "✶", "✳", "✢"]
MODE_BAR = {
    "default": ("muted", "? /help for commands · shift+tab to switch modes"),
    "accept-edits": ("edits", "⏵⏵ accept edits on (shift+tab to cycle)"),
    "plan": ("plan", "⏸ plan mode on (shift+tab to cycle)"),
    "yolo": ("err", "⚠ yolo mode: every action is auto-approved"),
}


def fmt_tokens(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def bullet(renderable, mark: str = "●", style: str = "white") -> Table:
    grid = Table.grid(padding=(0, 1))
    grid.add_column(width=1, no_wrap=True)
    grid.add_column(ratio=1)
    grid.add_row(Text(mark, style=style), renderable)
    return grid


def short_path(path: str) -> str:
    """Show paths inside the project relative to it (the agent always runs from the project root)."""
    cwd = os.getcwd().rstrip("/") + "/"
    path = str(path)
    for prefix in (cwd, "/private" + cwd if not cwd.startswith("/private") else cwd[len("/private"):]):
        if path.startswith(prefix):
            return path[len(prefix):] or "."
    return path


def primary_arg(name: str, args: dict, root=None) -> str:
    if name in ("read_file", "write_file", "edit_file"):
        extra = ""
        if name == "read_file" and ("offset" in args or "limit" in args):
            extra = f", lines {args.get('offset', 1)}+"
        return f"{short_path(args.get('path', ''))}{extra}"
    if name == "bash":
        command = " ".join(str(args.get("command", "")).split())
        return command if len(command) <= 90 else command[:87] + "..."
    if name == "glob":
        return args.get("pattern", "") + (f" in {args['path']}" if args.get("path") not in (None, ".") else "")
    if name == "grep":
        return f'"{args.get("pattern", "")}"' + (f" in {args['path']}" if args.get("path") not in (None, ".") else "")
    if name == "web_fetch":
        return str(args.get("url", ""))[:90]
    if name == "task":
        return str(args.get("description", ""))
    if name.startswith("mcp__") and args:
        first = str(next(iter(args.values())))
        return " ".join(first.split())[:60]
    return ""


def render_diff(diff: str, max_lines: int = 40) -> Text:
    out = Text()
    old_ln = new_ln = 0
    shown = total = 0
    for line in diff.splitlines():
        if line.startswith(("---", "+++")):
            continue
        if line.startswith("@@"):
            m = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)", line)
            if m:
                old_ln, new_ln = int(m[1]), int(m[2])
            if shown and shown < max_lines:
                out.append("        ⋮\n", style="muted")
            continue
        if line.startswith("\\"):
            continue
        total += 1
        if shown >= max_lines:
            continue
        shown += 1
        if line.startswith("+"):
            out.append(f"{new_ln:>6} + {line[1:]}\n", style="#b8f0c0 on #133a20")
            new_ln += 1
        elif line.startswith("-"):
            out.append(f"{old_ln:>6} - {line[1:]}\n", style="#ffc4c4 on #4a1a1a")
            old_ln += 1
        else:
            out.append(f"{new_ln:>6}   {line[1:]}\n", style="muted")
            old_ln += 1
            new_ln += 1
    if total > shown:
        out.append(f"        … {total - shown} more lines\n", style="muted")
    out.rstrip()
    return out


def todo_lines(todos: list[dict]) -> Text:
    out = Text()
    for t in todos:
        if t["status"] == "completed":
            out.append("☒ ", style="ok")
            out.append(t["content"], style="muted strike")
        elif t["status"] == "in_progress":
            out.append("◼ ", style="accent")
            out.append(t["content"], style="bold")
        else:
            out.append("☐ ", style="muted")
            out.append(t["content"])
        out.append("\n")
    out.rstrip()
    return out


def indent(renderable, spaces: int = 5) -> Table:
    grid = Table.grid()
    grid.add_column(width=spaces)
    grid.add_column(ratio=1)
    grid.add_row("", renderable)
    return grid


class StreamView:
    """Live view of a streaming response: a spinner while waiting, the text as it arrives."""

    def __init__(self, ui: "TerminalUI", quiet: bool = False):
        self.ui, self.console = ui, ui.console
        self.quiet = quiet  # a subagent's stream: spinner only, its text isn't printed
        self.text = self.thinking = ""
        self.tool: str | None = None
        self.verb = random.choice(VERBS)
        self.start = time.monotonic()
        self.think_start = self.think_end = 0.0
        self.chars = 0
        self.live = Live(console=self.console, get_renderable=self._render, refresh_per_second=12, transient=True)

    def __enter__(self) -> "StreamView":
        self.live.start()
        return self

    def update(self, kind: str, data) -> None:
        if kind == "text":
            self.text += data
        elif kind == "thinking":
            self.thinking += data
            self.think_start = self.think_start or time.monotonic()
            self.think_end = time.monotonic()
        elif kind == "tool_start":
            self.tool = data
        self.chars += len(data) if isinstance(data, str) else 0

    def _status(self) -> Text:
        elapsed = time.monotonic() - self.start
        frame = SPINNER[int(elapsed * 8) % len(SPINNER)]
        if self.tool:
            label = f"Preparing {TOOL_LABELS.get(self.tool, self.tool)}"
        elif self.thinking and not self.text:
            label = "Thinking"
        else:
            label = self.verb
        return Text.assemble((f"{frame} {label}… ", "accent"),
                             (f"({int(elapsed)}s · ↓ {fmt_tokens(self.chars // 4)} tokens · ctrl+c to interrupt)", "muted"))

    def _render(self):
        if self.quiet or not self.text.strip():
            return self._status()
        width = max(20, self.console.size.width - 4)
        budget = max(3, self.console.size.height - 4)
        lines = self.text.splitlines()
        rows = sum(max(1, math.ceil(len(line) / width)) for line in lines)
        body = Markdown(self.text) if rows <= budget else Text("\n".join(lines[-budget:]))
        return Group(bullet(body), Text(""), self._status())

    def __exit__(self, *exc) -> bool:
        self.live.stop()
        if self.quiet:
            return False
        if self.thinking.strip() and self.ui.show_thinking:
            self.ui.print_thinking(self.thinking, self.think_end - self.think_start)
        if self.text.strip():
            self.ui.print_assistant(self.text)
        return False


class TerminalUI:
    def __init__(self, console: Console | None = None, interactive: bool = True, show_thinking: bool = True):
        self.console = console or Console(theme=THEME, highlight=False)
        self.interactive = interactive
        self.show_thinking = show_thinking
        self._session = None

    # ------------------------------------------------------------------ output

    def banner(self, agent) -> None:
        title = Text.assemble(("✻ ", "accent"), "Welcome to ", ("nanocode", f"bold {ACCENT}"), "!")
        lines = [title, Text(""),
                 Text("/help for help · /model to switch models · shift+tab to switch modes", style="muted"),
                 Text(""), Text(f"cwd:   {agent.root}", style="muted"), Text(f"model: {agent.provider.label}", style="muted")]
        if agent.memory:
            lines.append(Text("memory: " + ", ".join(p.name for p, _ in agent.memory), style="muted"))
        self.console.print(Panel(Group(*lines), border_style="accent", expand=False, padding=(0, 2)))

    def print_assistant(self, text: str) -> None:
        self.console.print()
        self.console.print(bullet(Markdown(text)))

    def print_thinking(self, thinking: str, seconds: float = 0.0) -> None:
        """Collapsed like Claude Code: a one-line header and a short dimmed preview."""
        flat = " ".join(thinking.split())
        limit = max(120, (self.console.size.width - 8) * 2)  # about two lines
        preview = flat if len(flat) <= limit else flat[:limit].rsplit(" ", 1)[0] + " …"
        label = f"Thought for {seconds:.0f}s" if seconds >= 1 else "Thought"
        self.console.print()
        self.console.print(bullet(Text(label, style="muted italic"), mark="✻", style="muted"))
        self.console.print(indent(Text(preview, style="muted italic"), 2))

    def user_echo(self, text: str) -> None:
        self.console.print(Text.assemble(("❯ ", f"bold {ACCENT}"), text))

    def info(self, message: str) -> None:
        self.console.print(Text(f"  ✻ {message}", style="muted"))

    def warn(self, message: str) -> None:
        self.console.print(Text(f"  ⚠ {message}", style="warn"))

    def error(self, message: str) -> None:
        self.console.print(Text(f"  ✗ {message}", style="err"))

    def interrupted(self) -> None:
        self.console.print(Text("  ⎿  Interrupted · What should nanocode do instead?", style="err"))

    def compacted(self, before: int, after: int, limit: int, summary: str) -> None:
        self.console.print()
        self.console.print(bullet(Text.assemble(("Conversation compacted ", "bold"),
                                                (f"({fmt_tokens(before)} → {fmt_tokens(after)} tokens of "
                                                 f"{fmt_tokens(limit)})", "muted")), mark="✻", style="accent"))
        preview = "\n".join(summary.strip().splitlines()[:8])
        self.console.print(indent(Text(preview + "\n…", style="muted"), 2))

    @contextlib.contextmanager
    def stream_view(self, parent=None):
        with StreamView(self, quiet=parent is not None) as view:
            yield view

    @contextlib.contextmanager
    def spinner(self, label: str):
        with self.console.status(Text(f"{label}…", style="accent"), spinner="dots", spinner_style="accent"):
            yield

    @contextlib.contextmanager
    def tool_spinner(self, name: str, args: dict):
        if name in ("bash", "grep", "glob"):
            label = "Running" if name == "bash" else "Searching"
            with self.console.status(Text(f"{label}… (ctrl+c to interrupt)", style="muted"), spinner="dots",
                                     spinner_style="accent"):
                yield
        else:
            yield

    # ------------------------------------------------------------------ tools

    def tool_call(self, name: str, args: dict, call_id=None, parent=None) -> None:
        label, arg = tool_label(name), primary_arg(name, args)
        if parent is not None:  # inside a subagent: one compact, dimmed line
            self.console.print(Text(f"     ⎿ {label}({arg})" if arg else f"     ⎿ {label}", style="muted"))
            return
        header = Text.assemble((label, "bold"), (f"({arg})" if arg else "", ""))
        self.console.print()
        self.console.print(bullet(header, style="ok"))

    def tool_result(self, name: str, args: dict, result: ToolResult, todos: list | None = None, call_id=None,
                    parent=None) -> None:
        prefix = "  ⎿  "
        if parent is not None:
            if result.is_error:
                self.console.print(Text(f"       {(result.summary or result.output)[:120]}", style="muted"))
            return
        if name == "task" and not result.is_error:
            self.console.print(Text.assemble(prefix, (result.summary, "muted")))
            return
        if result.is_error:
            first = (result.summary or result.output).strip().splitlines()[0][:300]
            self.console.print(Text.assemble(prefix, (first, "err")))
            return
        if result.detail_kind == "todos" and todos is not None:
            self.console.print(Text(prefix), end="")
            self.console.print(indent(todo_lines(todos), 0) if todos else Text("(empty todo list)", style="muted"))
            return
        if result.detail_kind == "diff":
            self.console.print(Text.assemble(prefix, (result.summary, "muted")))
            if result.detail:
                self.console.print(indent(render_diff(result.detail, 30), 2))
            return
        if result.detail_kind == "text":
            lines = result.detail.splitlines()
            preview = "\n".join(line[:200] for line in lines[:5]) or "(no output)"
            text = Text(preview, style="muted")
            if len(lines) > 5:
                text.append(f"\n… +{len(lines) - 5} lines", style="muted")
            self.console.print(Text(prefix), end="")
            self.console.print(indent(text, 0))
            if result.summary:
                self.console.print(Text(f"     {result.summary}", style="err"))
            return
        self.console.print(Text.assemble(prefix, (result.summary or "Done", "muted")))

    # ------------------------------------------------------------------ questions

    def choose(self, question: str, options: list[tuple[str, str]], default: str | None = None) -> str:
        if not self.interactive:
            return options[-1][0]
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.shortcuts import choice
        from prompt_toolkit.styles import Style

        cancel = options[-1][0]  # the last option is always the safe one ("no" / "cancel")
        keys = KeyBindings()

        @keys.add("escape", eager=True)
        @keys.add("n")
        def _cancel(event):
            event.app.exit(result=cancel)

        style = Style.from_dict({"selected-option": f"{ACCENT} bold", "number": "#888888"})
        try:
            return choice(message=question, options=options, default=default or options[0][0], style=style,
                          symbol="❯", key_bindings=keys)
        except (KeyboardInterrupt, EOFError):
            return cancel

    def ask_text(self, label: str) -> str:
        if not self.interactive:
            return ""
        from prompt_toolkit import prompt

        try:
            return prompt(label).strip()
        except (KeyboardInterrupt, EOFError):
            return ""

    def ask_permission(self, name: str, args: dict, preview: str, permissions, call_id=None) -> tuple[str, str]:
        if not self.interactive:
            return "no", ("nanocode is running non-interactively, so this action could not be approved. "
                          "Finish without it, or tell the user which command to run (or to use --yolo).")
        req = describe_request(name, args, preview)
        if req["preview_kind"] == "diff":
            body = Group(Text(args.get("path", ""), style="bold"), Text(""), render_diff(preview, 60))
        else:
            body = Group(Text(f"  {req['preview']}", style="bold"))
        always = req["always_label"] + (" (shift+tab)" if name in ("write_file", "edit_file") else "")
        self.console.print(Panel(body, title=req["title"], title_align="left", border_style="accent", padding=(0, 1)))
        question = req["question"]
        answer = self.choose(question, [("yes", "Yes"), ("always", always),
                                        ("no", "No, and tell nanocode what to do differently (esc)")])
        feedback = self.ask_text("  ↳ What should nanocode do instead? (enter to just stop) ") if answer == "no" else ""
        return answer, feedback

    def ask_plan_approval(self, plan: str) -> tuple[str, str]:
        self.console.print(Panel(Markdown(plan), title="Ready to code?", title_align="left", border_style="plan",
                                 padding=(0, 1)))
        if not self.interactive:
            return "no", ""
        answer = self.choose("Would you like to proceed?", [("accept-edits", "Yes, and auto-accept edits"),
                                                            ("default", "Yes, and manually approve edits"),
                                                            ("no", "No, keep planning (esc)")])
        feedback = self.ask_text("  ↳ What should change in the plan? (enter to just stop) ") if answer == "no" else ""
        return answer, feedback

    # ------------------------------------------------------------------ input

    def read_input(self, agent) -> str:
        if self._session is None:
            self._session = self._make_session(agent)
        self.console.print()
        self.console.rule(style="grey27")
        return self._session.prompt([("class:prompt", "❯ ")])

    def _make_session(self, agent):
        from prompt_toolkit import PromptSession
        from prompt_toolkit.history import FileHistory, InMemoryHistory
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.styles import Style

        kb = KeyBindings()

        @kb.add("s-tab")
        def _cycle_mode(event):
            agent.permissions.cycle(allow_yolo=agent.settings.allow_yolo_cycle)
            event.app.invalidate()

        @kb.add("enter")
        def _submit(event):
            buf = event.current_buffer
            state = buf.complete_state
            if state and state.completions:
                completion = state.current_completion or state.completions[0]
                buf.apply_completion(completion)
                if not buf.text.startswith("/"):
                    return  # @file completion: keep typing
            if buf.text.endswith("\\"):  # backslash-enter inserts a newline
                buf.delete_before_cursor(1)
                buf.insert_text("\n")
                return
            buf.validate_and_handle()

        @kb.add("c-j")
        @kb.add("escape", "enter")
        def _newline(event):
            event.current_buffer.insert_text("\n")

        try:
            HOME.mkdir(parents=True, exist_ok=True)
            history = FileHistory(str(HOME / "history"))
        except OSError:
            history = InMemoryHistory()
        style = Style.from_dict({
            "prompt": f"{ACCENT} bold", "bottom-toolbar": "noreverse bg:default", "bar.muted": "#8a8a8a",
            "bar.edits": "#c678dd", "bar.plan": "#56b6c2", "bar.err": "#ff6b6b bold", "bar.dim": "#5f5f5f",
        })
        return PromptSession(history=history, key_bindings=kb, multiline=True, completer=InputCompleter(agent),
                             complete_while_typing=True, bottom_toolbar=lambda: self._toolbar(agent), style=style,
                             prompt_continuation="  ", reserve_space_for_menu=6)

    def _toolbar(self, agent):
        style, label = MODE_BAR.get(agent.permissions.mode, MODE_BAR["default"])
        limit = agent.context_limit()
        pct = min(100, round(100 * agent.context_used() / limit)) if limit else 0
        return [(f"class:bar.{style}", f"  {label}"), ("class:bar.dim", f"   {agent.provider.label} · {pct}% context")]


try:
    from prompt_toolkit.completion import Completer, Completion
except ImportError:  # pragma: no cover
    Completer, Completion = object, None


class InputCompleter(Completer):
    """Autocomplete /commands and @file mentions."""

    def __init__(self, agent):
        self.agent = agent
        self._files: list[str] = []
        self._files_at = 0.0

    def _file_list(self) -> list[str]:
        if time.monotonic() - self._files_at > 30:
            files = []
            for i, path in enumerate(walk_files(self.agent.root)):
                if i >= 5000:
                    break
                files.append(path.relative_to(self.agent.root).as_posix())
            self._files, self._files_at = files, time.monotonic()
        return self._files

    def get_completions(self, document, complete_event):
        from .commands import all_commands

        text = document.text_before_cursor
        if text.startswith("/") and " " not in text:
            for name, description in all_commands(self.agent):
                if name.startswith(text[1:]):
                    yield Completion("/" + name, start_position=-len(text), display_meta=description)
            return
        word = document.get_word_before_cursor(WORD=True)
        if word.startswith("@"):
            needle = word[1:].lower()
            hits = [p for p in self._file_list() if needle in p.lower()]
            hits.sort(key=lambda p: (not p.lower().startswith(needle), len(p)))
            for path in hits[:30]:
                yield Completion("@" + path, start_position=-len(word))
