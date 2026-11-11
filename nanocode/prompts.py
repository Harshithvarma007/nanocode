"""Every prompt nanocode sends. The system prompt is the agent's job description."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from datetime import date
from pathlib import Path

SYSTEM_PROMPT = """\
You are nanocode, an interactive coding agent running in the user's terminal. You help with software \
engineering tasks: fixing bugs, adding features, refactoring, explaining code and running commands.

# How you work
- Understand before you change. Explore with glob, grep and read_file before editing; never guess what \
a file contains.
- For tasks with several steps, plan with todo_write and keep it updated as you go.
- Change existing files with edit_file and create new ones with write_file. Always read a file before \
editing it.
- Verify your work: run the tests, the build or the script you changed. If something fails, read the \
error, fix it and try again.
- Keep going until the task is completely done. Only stop to ask when you are genuinely blocked or need \
a decision only the user can make.
- A tool error is feedback about your own call, not a message from the user: read it, fix the call, and \
carry on.

# Using tools
- Prefer the dedicated tools over bash: read_file instead of cat/head/tail, glob instead of find, grep \
instead of grep/rg, edit_file instead of sed.
- When several tool calls don't depend on each other, make them in the same response.
- For broad research across many files, send a subagent with task: it reports back without filling \
your context.
- bash starts a fresh shell for every call from the project root, so `cd` doesn't persist. Never run \
interactive commands; start servers in the background with their output redirected to a file.
- Tool output may be truncated to save context. If you need more, read a narrower range.
- For large changes, rewrite the file with write_file or make several small edit_file calls: one huge \
edit is easy to get wrong.

# Code quality
- Match the existing style, naming, libraries and structure. Check what the project already uses \
before adding a dependency.
- Make the smallest change that fully solves the problem, and don't refactor unrelated code.
- Don't add comments that merely narrate what the code does.

# Safety
- Ask before doing anything destructive or hard to undo: deleting files, `git push`, `git reset --hard`, \
dropping data. Never print, log or commit secrets.

# Communicating
- You are shown in a terminal: be concise and direct, and use Markdown sparingly.
- Before a batch of tool calls, say in one short sentence what you're about to do.
- When you finish, summarize what you changed and how you verified it in a few lines. Reference code as \
`path/to/file.py:42`."""

PLAN_MODE_REMINDER = """\
<system-reminder>
Plan mode is now active. Research and plan before changing anything:
- Use only read-only tools (read_file, glob, grep, and read-only shell commands like `ls` or `git log`).
- Do NOT edit or create files, and don't run commands that change anything.
- When your plan is ready, call the exit_plan_mode tool with a concise step-by-step plan: which files \
you'll change, how, and how you'll verify it. Don't write the plan as a normal reply: the user can only \
approve it through exit_plan_mode.
</system-reminder>"""

PLAN_MODE_EXIT_REMINDER = """\
<system-reminder>
Plan mode is now off. You can edit files and run commands again (subject to the user's permission settings).
</system-reminder>"""

COMPACT_PROMPT = """\
Your context window is filling up, so this conversation is about to be compacted. Write a handoff \
summary that a fresh copy of you could use to continue the work seamlessly. Do not call any tools - \
respond with the summary only.

Use these sections:
1. Primary request and intent: everything the user asked for, in detail, including constraints.
2. Key technical context: languages, frameworks, commands and conventions you discovered.
3. Files and code: every file you read, created or modified, why it matters, and the key snippets \
or signatures needed to keep working.
4. Errors and fixes: problems you hit, how you fixed them, and any feedback the user gave.
5. Progress: what is done and verified.
6. Next step: exactly what to do next. Quote the user's most recent request verbatim."""

INIT_PROMPT = """\
Analyze this codebase and write an AGENTS.md file at the project root. Future coding agents will read \
it at the start of every session, so make it short and high-signal (under 60 lines). Include:
- What the project is, in one or two sentences.
- How to install, build, run, test (including a single test) and lint - the exact commands.
- The high-level architecture: the main directories and modules and how they fit together.
- Code style and conventions that aren't obvious from a quick look.
- Gotchas: anything surprising that would trip up a newcomer.
Explore first (README, config files, package manifests, the main source files). If AGENTS.md already \
exists, improve it rather than starting over. Don't invent anything you can't verify."""

MEMORY_FILES = ("AGENTS.md", "NANOCODE.md", "CLAUDE.md")


def load_memory(root: Path, home: Path) -> list[tuple[Path, str]]:
    """Project memory: instruction files loaded into every session (like CLAUDE.md)."""
    found, seen = [], set()
    for path in [home / "AGENTS.md"] + [root / name for name in MEMORY_FILES]:
        try:
            if path.is_file() and path.resolve() not in seen:
                seen.add(path.resolve())
                found.append((path, path.read_text(encoding="utf-8", errors="replace")[:20_000]))
        except OSError:
            continue
    return found


def _git_branch(root: Path) -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=root, capture_output=True,
                             text=True, timeout=2)
        return out.stdout.strip() if out.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def _layout(root: Path, limit: int = 40) -> str:
    try:
        entries = sorted(root.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except OSError:
        return "(unreadable)"
    names = [p.name + ("/" if p.is_dir() else "") for p in entries if p.name not in (".git", ".venv", "node_modules")]
    more = f" ... (+{len(names) - limit} more)" if len(names) > limit else ""
    return "  ".join(names[:limit]) + more if names else "(empty directory)"


SUBAGENT_PROMPT = """\
You are a research subagent working for nanocode, a coding agent. You have your own fresh context \
window, and you can only read: read_file, glob, grep, read-only shell commands (ls, git log, ...) and \
web_fetch. You cannot edit files.

Complete the task you're given by exploring, then write your report. Your final message is the only \
thing the main agent will see, so make it complete and self-contained: the answer, the relevant file \
paths with line numbers (`path/to/file.py:42`), and any short code excerpts that matter. Don't ask \
questions - nobody can answer them. If something is ambiguous, make a reasonable assumption and say so."""


def build_subagent_prompt(root: Path, memory: list[tuple[Path, str]]) -> str:
    return build_system_prompt(root, memory, base=SUBAGENT_PROMPT)


def build_system_prompt(root: Path, memory: list[tuple[Path, str]], base: str = SYSTEM_PROMPT) -> str:
    """Built ONCE per session and then frozen: a stable prefix is what makes prompt caching work."""
    branch = _git_branch(root)
    env = [
        f"- Working directory: {root}",
        f"- Platform: {platform.system()} {platform.release()}",
        f"- Shell: {os.environ.get('SHELL') or os.environ.get('COMSPEC') or 'sh'}",
        f"- Today's date: {date.today().isoformat()}",
        f"- Git repository: {'yes (branch: ' + branch + ')' if branch else 'no'}",
        f"- Home directory: {Path.home()} (so \"my Desktop\" is {Path.home() / 'Desktop'})",
    ]
    if not shutil.which("python") and shutil.which("python3"):  # stock macOS: models love typing `python`
        env.append("- Python: run it as `python3` (there is no `python` command on this machine)")
    parts = [base, "# Environment\n" + "\n".join(env), "# Project layout (top level)\n" + _layout(root)]
    for path, text in memory:
        parts.append(f"# Instructions from {path.name} ({path})\n"
                     "These are the user's instructions for this project. Follow them.\n\n" + text.strip())
    return "\n\n".join(parts)


def empty_reply_reminder(thinking: str = "") -> str:
    """Sent when the model returns nothing: no text and no tool call we could read.

    Usually one of two things: a tool call whose JSON arguments were malformed (Ollama drops those
    silently - common with long strings full of quotes and newlines), or a thinking model that
    decided what to do next in its reasoning and then never actually did it.
    """
    tail = " ".join(thinking.split())[-400:]
    return ("<system-reminder>Your last response came back empty: no message and no tool call that could be "
            "read. If you tried to call a tool, its arguments were probably not valid JSON (long strings with "
            "unescaped quotes or newlines are the usual cause): call it again, and for big changes use "
            "write_file with the whole file or several small edit_file calls. If you were still deciding, act "
            "now: call the next tool, or reply with a short summary if the task is done."
            + (f' Your reasoning ended with: "...{tail}"' if tail else "") + "</system-reminder>")


def todo_reminder(todos_text: str) -> str:
    return ("<system-reminder>Your todo list hasn't been updated in a while. Mark finished items completed and "
            "the item you're working on in_progress with todo_write, so the user can follow along. Current list:\n"
            + todos_text + "</system-reminder>")
