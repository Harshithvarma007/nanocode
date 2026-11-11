"""Permissions and modes: deciding what the agent may do without asking.

    default       read-only tools run freely; edits and shell commands ask first
    accept-edits  file edits inside the project are auto-approved; shell commands still ask
    plan          read-only: the agent researches and proposes a plan, nothing changes
    yolo          everything is auto-approved (only enabled with --yolo)
    explore       (subagents only) read-only, no plan to propose: research and report back

Fetching a URL and calling an MCP tool always ask first (outside yolo); "don't ask again" then
remembers the domain or the tool for the rest of the session.
"""

from __future__ import annotations

import json
import re
import shlex
from urllib.parse import urlparse

from .tools import Tool, ToolContext

MODE_CYCLE = ("default", "accept-edits", "plan")
EDIT_TOOLS = {"write_file", "edit_file"}

# Commands that only read. Anything with shell metacharacters (pipes, ;, &&, redirects,
# $(...), backticks) is never auto-approved: `ls; rm -rf ~` must not sneak through as "ls".
SAFE_COMMANDS = {"ls", "pwd", "cat", "head", "tail", "wc", "echo", "which", "whoami", "date", "tree",
                 "file", "stat", "du", "df", "grep", "rg", "sort", "uniq", "diff", "uname"}
SAFE_GIT = {"status", "diff", "log", "show", "blame"}
SHELL_META = re.compile(r"[;&|<>`$\n\\]")
# For these tools the first two words identify the command ("npm test", "git commit").
MULTI_WORD = {"git", "npm", "pnpm", "yarn", "bun", "npx", "uv", "pip", "pip3", "python", "python3", "cargo",
              "go", "docker", "make", "poetry", "deno", "kubectl", "dotnet", "mvn", "gradle"}

DENIED = {
    "plan": ("Plan mode is active, so you can't modify files or run commands that change anything. "
             "Keep researching with read-only tools, then call exit_plan_mode with your plan."),
    "explore": ("Subagents are read-only: you can't modify files or run commands that change anything. "
                "Report what you found instead."),
}


def _tokens(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()


def is_safe_command(command: str) -> bool:
    if SHELL_META.search(command):
        return False
    tokens = _tokens(command)
    if not tokens:
        return False
    if tokens[0] in SAFE_COMMANDS:
        return True
    return tokens[0] == "git" and len(tokens) > 1 and tokens[1] in SAFE_GIT


def command_rule(command: str) -> str:
    """The 'family' a command belongs to, for "don't ask again for X commands"."""
    tokens = _tokens(command)
    if not tokens:
        return command
    if tokens[0] in MULTI_WORD and len(tokens) > 1:
        if tokens[1] == "-m" and len(tokens) > 2:  # python -m pytest
            return " ".join(tokens[:3])
        return " ".join(tokens[:2])
    return tokens[0]


def url_domain(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def mcp_parts(tool_name: str) -> tuple[str, str]:
    """'mcp__github__create_issue' -> ('github', 'create_issue')."""
    _, server, tool = (tool_name.split("__", 2) + ["", ""])[:3]
    return server, tool


def describe_request(tool_name: str, args: dict, preview: str) -> dict:
    """What a permission prompt says. Shared by the terminal UI and the desktop app."""
    if tool_name == "bash":
        command = args.get("command", "")
        return {"title": "Bash command", "question": "Do you want to run this command?", "preview": command,
                "preview_kind": "command",
                "always_label": f"Yes, and don't ask again for `{command_rule(command)}` commands this session"}
    if tool_name in EDIT_TOOLS:
        path = args.get("path", "")
        creating = tool_name == "write_file" and "@@ -0,0 " in preview
        return {"title": "Create file" if creating else "Edit file",
                "question": f"Do you want to {'create' if creating else 'make this edit to'} {path}?",
                "preview": preview, "preview_kind": "diff",
                "always_label": "Yes, allow all edits during this session"}
    if tool_name == "web_fetch":
        domain = url_domain(args.get("url", ""))
        return {"title": "Fetch", "question": f"Allow nanocode to fetch this page from {domain}?",
                "preview": args.get("url", ""), "preview_kind": "url",
                "always_label": f"Yes, and don't ask again for {domain}"}
    server, tool = mcp_parts(tool_name) if tool_name.startswith("mcp__") else ("", tool_name)
    return {"title": f"MCP · {server}" if server else tool_name,
            "question": f"Allow {server + ' to run ' if server else ''}{tool}?",
            "preview": json.dumps(args, indent=2)[:4000], "preview_kind": "json",
            "always_label": f"Yes, and don't ask again for {tool}"}


class Permissions:
    def __init__(self, mode: str = "default"):
        self.mode = mode
        self.allowed_commands: set[str] = set()
        self.allowed_domains: set[str] = set()
        self.allowed_tools: set[str] = set()

    def cycle(self, allow_yolo: bool = False) -> str:
        modes = MODE_CYCLE + (("yolo",) if allow_yolo else ())
        self.mode = modes[(modes.index(self.mode) + 1) % len(modes)] if self.mode in modes else "default"
        return self.mode

    def check(self, tool: Tool, args: dict, ctx: ToolContext) -> tuple[str, str]:
        """Returns ("allow" | "ask" | "deny", reason)."""
        if tool.read_only:
            return "allow", ""
        if tool.name == "web_fetch":  # reading the web is research, so it's fine in plan mode, with approval
            allowed = self.mode == "yolo" or url_domain(args.get("url", "")) in self.allowed_domains
            return ("allow", "") if allowed else ("ask", "")
        safe_bash = tool.name == "bash" and is_safe_command(args.get("command", ""))
        if self.mode in DENIED and not safe_bash:
            return "deny", DENIED[self.mode]
        if self.mode == "yolo" or safe_bash:
            return "allow", ""
        if tool.name in EDIT_TOOLS:
            inside = ctx.inside_root(ctx.resolve(args.get("path", "")))
            return ("allow", "") if self.mode == "accept-edits" and inside else ("ask", "")
        if tool.name == "bash":
            command = args.get("command", "")
            if not SHELL_META.search(command) and command_rule(command) in self.allowed_commands:
                return "allow", ""
            return "ask", ""
        return ("allow", "") if tool.name in self.allowed_tools else ("ask", "")

    def remember(self, tool_name: str, args: dict) -> None:
        """The user chose "yes, don't ask again"."""
        if tool_name in EDIT_TOOLS:
            self.mode = "accept-edits"
        elif tool_name == "bash":
            self.allowed_commands.add(command_rule(args.get("command", "")))
        elif tool_name == "web_fetch":
            self.allowed_domains.add(url_domain(args.get("url", "")))
        else:
            self.allowed_tools.add(tool_name)
