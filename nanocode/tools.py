"""The agent's tools: how the model touches the real world.

A tool is just three things: a name, a description (the model reads it!), and a JSON
schema for its arguments. Every tool returns a ToolResult: `output` is what the MODEL
sees; `summary` and `detail` are what the HUMAN sees in the terminal.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


class ToolError(Exception):
    """A failure the model should see - and can usually recover from."""


@dataclass
class ToolResult:
    output: str  # sent to the model
    summary: str = ""  # one line for the human ("Read 42 lines")
    detail: str = ""  # optional extra for the human: a diff or command output
    detail_kind: str = ""  # "diff" | "text" | "todos" | ""
    is_error: bool = False


@dataclass
class ToolContext:
    """State shared by every tool call in a session."""

    root: Path
    max_output_chars: int = 30_000
    read_files: dict = field(default_factory=dict)  # Path -> mtime_ns when we last read/wrote it
    todos: list = field(default_factory=list)
    cancel: threading.Event = field(default_factory=threading.Event)  # set by the desktop app's Stop button
    checkpoint: dict | None = None  # Path -> original text (None = didn't exist) for the running turn

    def remember_original(self, p: Path) -> None:
        """Checkpoints: save a file's content the first time a turn is about to change it."""
        if self.checkpoint is None or p in self.checkpoint:
            return
        try:
            self.checkpoint[p] = p.read_bytes().decode("utf-8") if p.exists() else None
        except (OSError, UnicodeDecodeError):
            pass  # not restorable as text; leave it out of the checkpoint

    def resolve(self, path: str) -> Path:
        p = Path(os.path.expanduser(str(path)))
        return (p if p.is_absolute() else self.root / p).resolve()

    def rel(self, p: Path) -> str:
        try:
            return p.relative_to(self.root).as_posix() or "."
        except ValueError:
            return str(p)

    def inside_root(self, p: Path) -> bool:
        try:
            p.relative_to(self.root)
            return True
        except ValueError:
            return False

    def mark_read(self, p: Path) -> None:
        self.read_files[p] = p.stat().st_mtime_ns

    def check_fresh(self, p: Path) -> None:
        """Never let the model overwrite a file it hasn't seen, or one that changed under it."""
        if p not in self.read_files:
            raise ToolError(f"You must read {self.rel(p)} with read_file before modifying it.")
        if p.stat().st_mtime_ns != self.read_files[p]:
            raise ToolError(f"{self.rel(p)} has changed since you last read it. Read it again before modifying it.")


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    fn: Callable
    read_only: bool = False

    def schema(self) -> dict:
        return {"name": self.name, "description": self.description, "parameters": self.parameters}


TOOLS: dict[str, Tool] = {}


def tool(name: str, description: str, parameters: dict, read_only: bool = False):
    def register(fn: Callable) -> Callable:
        TOOLS[name] = Tool(name, " ".join(description.split()), parameters, fn, read_only)
        return fn

    return register


def obj(properties: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": properties, "required": required}


# --------------------------------------------------------------------------- helpers

IGNORED_DIRS = {".git", "node_modules", ".venv", "venv", "env", "__pycache__", ".mypy_cache", ".pytest_cache",
                ".ruff_cache", ".tox", "dist", "build", ".next", ".nuxt", "target", ".idea", ".gradle", "coverage"}


def walk_files(base: Path):
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if d not in IGNORED_DIRS and not d.endswith(".egg-info"))
        for name in sorted(filenames):
            yield Path(dirpath) / name


def truncate_middle(text: str, limit: int) -> str:
    """Keep the head and the tail: errors usually live at the end of the output."""
    if len(text) <= limit:
        return text
    head, tail = text[: limit * 2 // 3], text[-(limit // 3):]
    cut = text[len(head): len(text) - len(tail)]
    return f"{head}\n\n... [{cut.count(chr(10))} lines ({len(cut)} chars) truncated to save context] ...\n\n{tail}"


def glob_to_regex(pattern: str) -> re.Pattern:
    """Translate a glob ('src/**/*.py', '*.{js,ts}') to a regex over '/'-separated paths."""
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
            continue
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
            continue
        c = pattern[i]
        if c == "*":
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "{" and "}" in pattern[i:]:
            j = pattern.index("}", i)
            out.append("(?:" + "|".join(re.escape(x) for x in pattern[i + 1: j].split(",")) + ")")
            i = j + 1
            continue
        elif c == "[" and "]" in pattern[i + 1:]:
            j = pattern.index("]", i + 1)
            out.append(pattern[i: j + 1])
            i = j + 1
            continue
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile("^" + "".join(out) + "$")


def _matcher(pattern: str):
    """Patterns without a '/' match file names at any depth (what models almost always mean).
    A comma-separated list ('*.html, *.css') matches any of them - small models write those a lot."""
    if "," in pattern and "{" not in pattern:
        matchers = [_matcher(p.strip()) for p in pattern.split(",") if p.strip()]
        return lambda rel_path, name: any(m(rel_path, name) for m in matchers)
    pattern = pattern[2:] if pattern.startswith("./") else pattern
    regex = glob_to_regex(pattern)
    if "/" in pattern:
        return lambda rel_path, name: bool(regex.match(rel_path))
    return lambda rel_path, name: bool(regex.match(name))


def _read_text(p: Path) -> str:
    data = p.read_bytes()
    if b"\0" in data[:8192]:
        raise ToolError(f"{p.name} looks like a binary file; it can't be read as text.")
    return data.decode("utf-8", errors="replace")


def make_diff(old: str, new: str, label: str) -> str:
    def lines(text: str) -> list[str]:
        return [line if line.endswith("\n") else line + "\n" for line in text.splitlines(keepends=True)]

    return "".join(difflib.unified_diff(lines(old), lines(new), fromfile=label, tofile=label, n=3))


def diff_stats(diff: str) -> tuple[int, int]:
    adds = sum(1 for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++"))
    rems = sum(1 for line in diff.splitlines() if line.startswith("-") and not line.startswith("---"))
    return adds, rems


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _numbered(lines: list[str], start: int) -> str:
    return "\n".join(f"{i:>6}\t{line}" for i, line in enumerate(lines, start))


# --------------------------------------------------------------------------- file tools

@tool("read_file", """Read a text file. Returns its lines prefixed with line numbers (like `cat -n`);
the numbers are not part of the file. Reads up to 2000 lines by default - for big files, use offset
and limit to read a specific range. Always read a file before editing it.""",
      obj({"path": {"type": "string", "description": "File path, relative to the project root or absolute"},
           "offset": {"type": "integer", "description": "Line number to start reading from (1-based)"},
           "limit": {"type": "integer", "description": "Maximum number of lines to read"}}, ["path"]),
      read_only=True)
def read_file(ctx: ToolContext, path: str, offset: int = 1, limit: int = 2000) -> ToolResult:
    p = ctx.resolve(path)
    if not p.exists():
        siblings = [f.name for f in p.parent.iterdir()] if p.parent.is_dir() else []
        close = difflib.get_close_matches(p.name, siblings, n=3)
        hint = f" Did you mean: {', '.join(close)}?" if close else ""
        raise ToolError(f"File not found: {path}.{hint}")
    if p.is_dir():
        raise ToolError(f"{path} is a directory. Use glob to list files in it.")
    lines = _read_text(p).splitlines()
    ctx.mark_read(p)
    if not lines:
        return ToolResult("(the file is empty)", summary="Read 0 lines (empty file)")

    start = max(1, offset)
    chosen, used = [], 0
    for line in lines[start - 1: start - 1 + max(1, limit)]:
        line = line if len(line) <= 2000 else line[:2000] + "... [line truncated]"
        if used + len(line) > ctx.max_output_chars and chosen:
            break  # respect the context budget: stop early and say where to continue
        chosen.append(line)
        used += len(line) + 8
    out = _numbered(chosen, start)
    end = start + len(chosen) - 1
    if end < len(lines):
        out += f"\n\n[Showing lines {start}-{end} of {len(lines)}. Use offset={end + 1} to read more.]"
    return ToolResult(out, summary=f"Read {_plural(len(chosen), 'line')}")


def apply_edit(text: str, old: str, new: str, replace_all: bool, label: str) -> str:
    if old == new:
        raise ToolError("Your edit_file call had identical old_string and new_string, so it would change nothing. "
                        "Work out what the new text should be and call edit_file again, or move on.")
    if not old:
        raise ToolError("old_string is empty. Use write_file to create a file, or include a nearby line in old_string.")
    count = text.count(old)
    if count == 0 and "\r\n" in text:  # the file uses Windows line endings
        old, new = old.replace("\n", "\r\n"), new.replace("\n", "\r\n")
        count = text.count(old)
    if count == 0:
        hint = ""
        stripped = old.strip()
        if stripped and stripped in text:
            hint = " A match exists with different leading/trailing whitespace."
        elif stripped and stripped.splitlines()[0].strip() in text:
            hint = " The first line exists in the file, but the following lines don't match exactly."
        raise ToolError(f"old_string was not found in {label}.{hint} Re-read the file and copy the text exactly, "
                        "including indentation (without the line-number prefix).")
    if count > 1 and not replace_all:
        raise ToolError(f"old_string matches {count} places in {label}. Include more surrounding lines to make it "
                        "unique, or set replace_all to true.")
    return text.replace(old, new) if replace_all else text.replace(old, new, 1)


def plan_change(ctx: ToolContext, name: str, args: dict) -> tuple[Path, str, str, bool]:
    """Work out (path, old_text, new_text, existed) for a write/edit WITHOUT touching disk.
    Used both for the permission-prompt preview and for actually applying the change."""
    p = ctx.resolve(args["path"])
    if p.is_dir():
        raise ToolError(f"{args['path']} is a directory.")
    if name == "write_file":
        if p.exists():
            ctx.check_fresh(p)
            return p, _read_text(p), args["content"], True
        return p, "", args["content"], False
    if not p.exists():
        raise ToolError(f"File not found: {args['path']}. Use write_file to create new files.")
    ctx.check_fresh(p)
    old = _read_text(p)
    new = apply_edit(old, args["old_string"], args["new_string"], bool(args.get("replace_all")), ctx.rel(p))
    return p, old, new, True


def preview_change(ctx: ToolContext, name: str, args: dict) -> str:
    """The diff shown when we ask the human for permission ('' for non-file tools)."""
    if name not in ("write_file", "edit_file"):
        return ""
    p, old, new, _ = plan_change(ctx, name, args)
    return make_diff(old, new, ctx.rel(p))


def _snippet(text: str, needle: str, context: int = 3) -> str:
    if not needle or needle not in text:
        return ""
    first = text[: text.index(needle)].count("\n")
    lines = text.splitlines()
    lo, hi = max(0, first - context), min(len(lines), first + needle.count("\n") + 1 + context)
    return _numbered(lines[lo:hi], lo + 1)


def _apply_change(ctx: ToolContext, name: str, args: dict) -> ToolResult:
    p, old, new, existed = plan_change(ctx, name, args)
    ctx.remember_original(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(new, encoding="utf-8", newline="")
    ctx.mark_read(p)
    rel = ctx.rel(p)
    diff = make_diff(old, new, rel)
    adds, rems = diff_stats(diff)
    if not existed:
        n = len(new.splitlines())
        return ToolResult(f"Created {rel} ({_plural(n, 'line')}).", summary=f"Wrote {_plural(n, 'line')} to {rel}",
                          detail=diff, detail_kind="diff")
    summary = f"Updated {rel} with {_plural(adds, 'addition')} and {_plural(rems, 'removal')}"
    if name == "write_file":
        return ToolResult(f"Overwrote {rel} ({_plural(len(new.splitlines()), 'line')}).", summary=summary,
                          detail=diff, detail_kind="diff")
    snippet = _snippet(new, args["new_string"])
    out = f"Edited {rel}." + (f" The edited region now reads:\n{snippet}" if snippet else "")
    return ToolResult(out, summary=summary, detail=diff, detail_kind="diff")


@tool("write_file", """Create a new file, or completely overwrite an existing one. Parent directories are
created automatically. To change part of an existing file, prefer edit_file. To overwrite an existing
file you must have read it first.""",
      obj({"path": {"type": "string", "description": "File path, relative to the project root or absolute"},
           "content": {"type": "string", "description": "The full content of the file"}}, ["path", "content"]))
def write_file(ctx: ToolContext, path: str, content: str) -> ToolResult:
    return _apply_change(ctx, "write_file", {"path": path, "content": content})


@tool("edit_file", """Replace an exact string in a file. old_string must match the file exactly - including
whitespace and indentation, without the line-number prefix that read_file shows - and must be unique
in the file unless replace_all is true. Include a few surrounding lines to make it unique. You must
read the file before editing it.""",
      obj({"path": {"type": "string", "description": "File path, relative to the project root or absolute"},
           "old_string": {"type": "string", "description": "The exact text to replace"},
           "new_string": {"type": "string", "description": "The text to replace it with"},
           "replace_all": {"type": "boolean", "description": "Replace every occurrence (default false)"}},
          ["path", "old_string", "new_string"]))
def edit_file(ctx: ToolContext, path: str, old_string: str, new_string: str, replace_all: bool = False) -> ToolResult:
    return _apply_change(ctx, "edit_file", {"path": path, "old_string": old_string,
                                            "new_string": new_string, "replace_all": replace_all})


# --------------------------------------------------------------------------- shell

def _kill(proc: subprocess.Popen) -> None:
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)  # kill the whole process group, children included
        else:
            proc.kill()
    except (ProcessLookupError, PermissionError):
        pass


@tool("bash", """Run a shell command from the project root and return its combined stdout/stderr and exit
code. Each call starts a fresh shell, so `cd` does not persist - use `cd dir && command`. Use it to run
tests, builds, scripts, git and package managers. Don't use it to read, search or edit files (use the
dedicated tools). Never run interactive commands. Start long-running servers in the background with
their output redirected, e.g. `npm run dev > dev.log 2>&1 &`.""",
      obj({"command": {"type": "string", "description": "The shell command to run"},
           "timeout": {"type": "integer", "description": "Seconds before the command is killed (default 120, max 600)"}},
          ["command"]))
def bash(ctx: ToolContext, command: str, timeout: int = 120) -> ToolResult:
    timeout = max(1, min(int(timeout), 600))
    env = {**os.environ, "PAGER": "cat", "GIT_PAGER": "cat", "GIT_TERMINAL_PROMPT": "0", "NANOCODE": "1"}
    proc = subprocess.Popen(command, shell=True, cwd=ctx.root, env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace",
                            start_new_session=(os.name == "posix"))
    deadline = time.monotonic() + timeout
    timed_out = False
    try:
        while True:  # wait in short slices so a Stop from the desktop app can kill the command
            try:
                out, _ = proc.communicate(timeout=0.25)
                break
            except subprocess.TimeoutExpired:
                if ctx.cancel.is_set():
                    raise KeyboardInterrupt
                if time.monotonic() > deadline:
                    _kill(proc)
                    out, _ = proc.communicate()
                    timed_out = True
                    break
    except KeyboardInterrupt:  # ctrl+c or Stop: take the command (and its children) down with us
        _kill(proc)
        proc.communicate()
        raise
    out = (out or "").rstrip()
    model_out = truncate_middle(out, ctx.max_output_chars) or "(no output)"
    if timed_out:
        model_out += f"\n[Command timed out after {timeout}s and was killed. Run long-running processes in the background.]"
        return ToolResult(model_out, summary=f"Timed out after {timeout}s", detail=out, detail_kind="text")
    model_out += f"\n[exit code {proc.returncode}]"
    summary = "" if proc.returncode == 0 else f"Exit code {proc.returncode}"
    return ToolResult(model_out, summary=summary, detail=out, detail_kind="text")


# --------------------------------------------------------------------------- search

@tool("glob", """Find files by name pattern, e.g. '**/*.py', 'src/**/*.ts' or 'test_*.py'. A pattern
without a '/' matches file names at any depth. Results are sorted by most recently modified. Skips
.git, node_modules, virtualenvs and build folders.""",
      obj({"pattern": {"type": "string", "description": "Glob pattern"},
           "path": {"type": "string", "description": "Directory to search in (default: project root)"}}, ["pattern"]),
      read_only=True)
def glob(ctx: ToolContext, pattern: str, path: str = ".") -> ToolResult:
    base = ctx.resolve(path)
    if not base.is_dir():
        raise ToolError(f"Not a directory: {path}")
    match = _matcher(pattern)
    found = [f for f in walk_files(base) if match(f.relative_to(base).as_posix(), f.name)]

    def mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0

    found.sort(key=mtime, reverse=True)
    if not found:
        return ToolResult("No files found.", summary="Found 0 files")
    shown = found[:200]
    out = "\n".join(ctx.rel(p) for p in shown)
    if len(found) > len(shown):
        out += f"\n... and {len(found) - len(shown)} more. Use a more specific pattern."
    return ToolResult(out, summary=f"Found {_plural(len(found), 'file')}")


def _grep_ripgrep(ctx: ToolContext, rg: str, pattern: str, base: Path, glob_pat: str | None,
                  ignore_case: bool, mode: str) -> list[str]:
    args = [rg, "--no-heading", "--color=never", "--max-columns=300", "--max-columns-preview"]
    if ignore_case:
        args.append("-i")
    if glob_pat:
        args += ["--glob", glob_pat]
    args += {"files_with_matches": ["-l"], "count": ["-c"], "content": ["-n"]}[mode]
    args += ["-e", pattern, os.path.relpath(base, ctx.root)]
    proc = subprocess.run(args, cwd=ctx.root, capture_output=True, text=True, errors="replace", timeout=60)
    if proc.returncode == 2 and not proc.stdout:
        raise ToolError(proc.stderr.strip()[:500] or "ripgrep failed")
    return [line.removeprefix("./") for line in proc.stdout.splitlines()]


def _grep_python(ctx: ToolContext, pattern: str, base: Path, glob_pat: str | None,
                 ignore_case: bool, mode: str, limit: int) -> list[str]:
    try:
        regex = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    except re.error as e:
        raise ToolError(f"Invalid regular expression: {e}") from e
    match = _matcher(glob_pat) if glob_pat else None
    files = [base] if base.is_file() else walk_files(base)
    results: list[str] = []
    for f in files:
        if match and not match(f.relative_to(base).as_posix() if base.is_dir() else f.name, f.name):
            continue
        try:
            if f.stat().st_size > 2_000_000:
                continue
            data = f.read_bytes()
        except OSError:
            continue
        if b"\0" in data[:4096]:
            continue
        hits = 0
        for n, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), 1):
            if regex.search(line):
                hits += 1
                if mode == "content":
                    results.append(f"{ctx.rel(f)}:{n}:{line.strip()[:300]}")
                elif mode == "files_with_matches":
                    break
        if hits and mode == "files_with_matches":
            results.append(ctx.rel(f))
        elif hits and mode == "count":
            results.append(f"{ctx.rel(f)}:{hits}")
        if len(results) >= limit * 2:
            break
    return results


@tool("grep", """Search file contents with a regular expression. By default returns the paths of matching
files; set output_mode to 'content' to see matching lines with line numbers, or 'count' for the number
of matches per file. Narrow the search with path and with glob (e.g. '*.py').""",
      obj({"pattern": {"type": "string", "description": "Regular expression to search for"},
           "path": {"type": "string", "description": "File or directory to search (default: project root)"},
           "glob": {"type": "string", "description": "Only search files matching this glob, e.g. '*.ts'"},
           "ignore_case": {"type": "boolean", "description": "Case-insensitive search"},
           "output_mode": {"type": "string", "enum": ["files_with_matches", "content", "count"],
                           "description": "What to return (default files_with_matches)"},
           "head_limit": {"type": "integer", "description": "Maximum number of result lines (default 100)"}},
          ["pattern"]),
      read_only=True)
def grep(ctx: ToolContext, pattern: str, path: str = ".", glob: str | None = None, ignore_case: bool = False,
         output_mode: str = "files_with_matches", head_limit: int = 100) -> ToolResult:
    base = ctx.resolve(path)
    if not base.exists():
        raise ToolError(f"Path not found: {path}")
    rg = shutil.which("rg")
    if rg:
        results = _grep_ripgrep(ctx, rg, pattern, base, glob, ignore_case, output_mode)
    else:
        results = _grep_python(ctx, pattern, base, glob, ignore_case, output_mode, head_limit)
    if not results:
        return ToolResult("No matches found.", summary="Found 0 matches")
    total = len(results)
    shown = results[: max(1, head_limit)]
    out = truncate_middle("\n".join(shown), ctx.max_output_chars)
    if total > len(shown):
        out += f"\n... [{total - len(shown)} more results. Narrow the search or raise head_limit.]"
    noun = "file" if output_mode != "content" else "match"
    return ToolResult(out, summary=f"Found {_plural(total, noun)}")


# --------------------------------------------------------------------------- planning

@tool("todo_write", """Create or update the todo list for the current task. Use it for any task with three or
more steps: it keeps you on track and shows the user your progress. Always send the complete list.
Keep exactly one item in_progress while you work, and mark each item completed as soon as it's done.
Skip it for simple one-step tasks.""",
      obj({"todos": {"type": "array", "description": "The complete, updated todo list",
                     "items": obj({"content": {"type": "string", "description": "What to do, e.g. 'Add input validation'"},
                                   "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}},
                                  ["content", "status"])}}, ["todos"]),
      read_only=True)
def todo_write(ctx: ToolContext, todos: list) -> ToolResult:
    clean = []
    for item in todos:
        if not isinstance(item, dict) or not isinstance(item.get("content"), str):
            raise ToolError("Each todo needs a 'content' string and a 'status'.")
        status = item.get("status", "pending")
        if status not in ("pending", "in_progress", "completed"):
            raise ToolError(f"Invalid status '{status}'. Use pending, in_progress or completed.")
        clean.append({"content": item["content"], "status": status})
    ctx.todos = clean
    done = sum(t["status"] == "completed" for t in clean)
    return ToolResult(f"Todo list updated ({done}/{len(clean)} completed). Keep it up to date as you work.",
                      summary=f"{done}/{len(clean)} completed", detail_kind="todos")


@tool("exit_plan_mode", """Use only in plan mode, when you have finished researching and have a concrete plan.
Presents the plan to the user for approval. The plan should be concise Markdown: the files you will
change, what you will change in each, and how you will verify the result.""",
      obj({"plan": {"type": "string", "description": "The implementation plan, in Markdown"}}, ["plan"]),
      read_only=True)
def exit_plan_mode(ctx: ToolContext, plan: str) -> ToolResult:
    return ToolResult("Plan received.")  # the agent loop handles approval; see Agent._exit_plan_mode


@tool("task", """Launch a subagent: a helper with its own fresh context window that researches something and
reports back. Use it for broad exploration - finding where something is implemented across many files,
surveying an unfamiliar codebase, reading documentation - so the details don't fill up your own context.
Subagents are read-only: they can read, search, run read-only commands and fetch pages, but not edit.
It can't see this conversation, so give it a complete, self-contained prompt. Its final report comes
back to you as this tool's result.""",
      obj({"description": {"type": "string", "description": "A 3-5 word label, e.g. 'Find the auth middleware'"},
           "prompt": {"type": "string", "description": "The full task, with all the context the subagent needs"}},
          ["description", "prompt"]),
      read_only=True)
def task(ctx: ToolContext, description: str, prompt: str) -> ToolResult:
    return ToolResult("Subagent finished.")  # the agent loop runs it; see Agent._run_subagent


# --------------------------------------------------------------------------- web

@tool("web_fetch", """Fetch a web page or file over HTTP(S) and return it as text (HTML is converted to readable
text, with links kept). Use it for documentation, issues or API references the user points you to, or
that you need to finish the task. The user approves each new domain.""",
      obj({"url": {"type": "string", "description": "The full http:// or https:// URL"},
           "max_chars": {"type": "integer", "description": "Maximum characters to return (default: the context budget)"}},
          ["url"]))
def web_fetch(ctx: ToolContext, url: str, max_chars: int | None = None) -> ToolResult:
    from .webfetch import fetch

    limit = min(max_chars or ctx.max_output_chars, ctx.max_output_chars)
    text, summary = fetch(url, limit, cancel=ctx.cancel)
    return ToolResult(text, summary=summary, detail=text[:4000], detail_kind="text")


# --------------------------------------------------------------------------- validation & dispatch

_JSON_TYPES = {"string": str, "boolean": bool, "array": list, "object": dict}


def _coerce(value, expected):
    """Small local models often send '10' for 10 or 'true' for true. Be forgiving at the boundary."""
    if isinstance(expected, list):  # e.g. ["string", "null"] in MCP schemas
        return value
    if expected == "integer":
        if isinstance(value, str) and value.strip().lstrip("-").isdigit():
            return int(value)
        if isinstance(value, float) and value.is_integer():
            return int(value)
    if expected == "boolean" and isinstance(value, str) and value.lower() in ("true", "false"):
        return value.lower() == "true"
    if expected in ("array", "object") and isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, _JSON_TYPES[expected]):
                return parsed
        except ValueError:
            pass
    return value


def _type_ok(value, expected) -> bool:
    if expected is None:
        return True
    if isinstance(expected, list):
        return any(_type_ok(value, e) for e in expected) or (value is None and "null" in expected)
    if expected == "null":
        return value is None
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, _JSON_TYPES.get(expected, object))


def validate_args(t: Tool, args) -> tuple[dict | None, str | None]:
    """Check the model's arguments against the schema. Returns (clean_args, error_message)."""
    if not isinstance(args, dict):
        return None, "Tool arguments must be a JSON object."
    if "__invalid_json__" in args:
        return None, f"Your tool arguments were not valid JSON: {str(args['__invalid_json__'])[:300]}"
    props, required = t.parameters.get("properties", {}), t.parameters.get("required", [])
    clean = {}
    for key, value in args.items():
        if key not in props or (value is None and key not in required):
            continue  # ignore unknown or null optional arguments
        expected = props[key].get("type")
        value = _coerce(value, expected)
        if not _type_ok(value, expected):
            return None, f"Argument '{key}' should be of type {expected}, but got {type(value).__name__}."
        if "enum" in props[key] and value not in props[key]["enum"]:
            return None, f"Argument '{key}' must be one of: {', '.join(props[key]['enum'])}."
        clean[key] = value
    missing = [k for k in required if k not in clean]
    if missing:
        return None, f"Missing required argument(s): {', '.join(missing)}."
    return clean, None


def execute(ctx: ToolContext, name: str, args: dict, tool: Tool | None = None) -> ToolResult:
    try:
        result = (tool or TOOLS[name]).fn(ctx, **args)
    except ToolError as e:
        return ToolResult(f"Error: {e}", summary=str(e), is_error=True)
    except Exception as e:  # a crashing tool must never crash the agent
        return ToolResult(f"Error: {type(e).__name__}: {e}", summary=f"{type(e).__name__}: {e}", is_error=True)
    return result if isinstance(result, ToolResult) else ToolResult(str(result))
