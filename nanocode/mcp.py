"""A from-scratch MCP client (stdio transport).

MCP (the Model Context Protocol) lets any program offer tools to any agent. An MCP server is a
process that speaks JSON-RPC 2.0, one JSON object per line, on its stdin/stdout:

    -> {"jsonrpc":"2.0","id":1,"method":"initialize","params":{...}}
    <- {"jsonrpc":"2.0","id":1,"result":{"serverInfo":{...},"capabilities":{...}}}
    -> {"jsonrpc":"2.0","method":"notifications/initialized"}
    -> {"jsonrpc":"2.0","id":2,"method":"tools/list"}
    -> {"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"...","arguments":{...}}}

Configure servers like Claude Code's .mcp.json, in ~/.nanocode/mcp.json, <project>/.mcp.json or
<project>/.nanocode/mcp.json:

    {"mcpServers": {"github": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"],
                               "env": {"GITHUB_TOKEN": "..."}}}}

Each server tool becomes an agent tool named mcp__<server>__<tool>. They always ask permission.
"""

from __future__ import annotations

import collections
import itertools
import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

from . import __version__
from .tools import Tool, ToolError, ToolResult, truncate_middle

PROTOCOL_VERSION = "2025-06-18"


class McpError(Exception):
    pass


def tool_name(server: str, tool: str) -> str:
    clean = lambda s: re.sub(r"[^A-Za-z0-9_-]", "_", s)  # noqa: E731 - tool names must match ^[a-zA-Z0-9_-]{1,64}$
    return f"mcp__{clean(server)}__{clean(tool)}"[:64]


class McpServer:
    def __init__(self, name: str, command: str, args: list[str] | None = None, env: dict | None = None,
                 cwd: Path | None = None):
        self.name, self.command, self.args = name, command, list(args or [])
        self.env, self.cwd = dict(env or {}), cwd
        self.proc: subprocess.Popen | None = None
        self.tools: list[dict] = []
        self.status, self.error = "starting", ""
        self._ids = itertools.count(1)
        self._pending: dict[int, list] = {}
        self._write_lock = threading.Lock()
        self._stderr = collections.deque(maxlen=40)

    # ------------------------------------------------------------------ lifecycle

    def start(self, timeout: float = 30.0) -> None:
        self.proc = subprocess.Popen([self.command, *self.args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                                     bufsize=1, cwd=self.cwd, env={**os.environ, **self.env},
                                     start_new_session=(os.name == "posix"))
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()
        self.request("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                    "clientInfo": {"name": "nanocode", "version": __version__}}, timeout)
        self.notify("notifications/initialized")
        tools, cursor = [], None
        while True:
            page = self.request("tools/list", {"cursor": cursor} if cursor else {}, timeout)
            tools += page.get("tools", [])
            cursor = page.get("nextCursor")
            if not cursor:
                break
        self.tools, self.status = tools, "connected"

    def close(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=2)
            except Exception:
                self.proc.kill()
        self.status = "disabled" if self.status == "starting" else self.status

    # ------------------------------------------------------------------ JSON-RPC

    def _send(self, message: dict) -> None:
        if not self.proc or self.proc.poll() is not None:
            raise McpError(f"MCP server '{self.name}' is not running.{self._stderr_tail()}")
        with self._write_lock:
            self.proc.stdin.write(json.dumps(message) + "\n")
            self.proc.stdin.flush()

    def notify(self, method: str, params: dict | None = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, **({"params": params} if params else {})})

    def request(self, method: str, params: dict, timeout: float = 60.0, cancel: threading.Event | None = None) -> dict:
        request_id = next(self._ids)
        slot = [threading.Event(), None]
        self._pending[request_id] = slot
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        try:
            while not slot[0].wait(0.1):
                if cancel is not None and cancel.is_set():
                    self.notify("notifications/cancelled", {"requestId": request_id, "reason": "interrupted"})
                    raise KeyboardInterrupt
                if self.proc.poll() is not None:
                    raise McpError(f"MCP server '{self.name}' exited.{self._stderr_tail()}")
                if time.monotonic() > deadline:
                    raise McpError(f"MCP server '{self.name}' didn't answer {method} within {timeout:.0f}s.")
        finally:
            self._pending.pop(request_id, None)
        message = slot[1]
        if "error" in message:
            raise McpError(message["error"].get("message", "unknown error"))
        return message.get("result") or {}

    def _read_stdout(self) -> None:
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                continue  # not protocol output (some servers log to stdout); ignore it
            if "id" in message and ("result" in message or "error" in message):
                slot = self._pending.get(message["id"])
                if slot:
                    slot[1] = message
                    slot[0].set()
            elif "id" in message and "method" in message:  # a request from the server to us
                reply = {"result": {}} if message["method"] == "ping" else \
                    {"error": {"code": -32601, "message": f"nanocode doesn't support {message['method']}"}}
                try:
                    with self._write_lock:
                        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": message["id"], **reply}) + "\n")
                        self.proc.stdin.flush()
                except OSError:
                    return

    def _read_stderr(self) -> None:
        for line in self.proc.stderr:
            self._stderr.append(line.rstrip())

    def _stderr_tail(self) -> str:
        return (" Last output: " + " | ".join(list(self._stderr)[-3:])) if self._stderr else ""

    # ------------------------------------------------------------------ tools

    def call(self, tool: str, arguments: dict, limit: int, cancel: threading.Event | None = None) -> ToolResult:
        try:
            result = self.request("tools/call", {"name": tool, "arguments": arguments}, timeout=300, cancel=cancel)
        except McpError as e:
            raise ToolError(str(e)) from e
        parts = []
        for item in result.get("content") or []:
            kind = item.get("type")
            if kind == "text":
                parts.append(item.get("text", ""))
            elif kind == "image":
                parts.append(f"[image: {item.get('mimeType', 'image')}, not shown]")
            elif kind == "resource":
                res = item.get("resource") or {}
                parts.append(res.get("text") or f"[resource: {res.get('uri', '')}]")
            elif kind == "resource_link":
                parts.append(f"[resource: {item.get('uri', '')}]")
        if not parts and result.get("structuredContent") is not None:
            parts.append(json.dumps(result["structuredContent"], indent=2))
        text = truncate_middle("\n".join(parts).strip() or "(no output)", limit)
        is_error = bool(result.get("isError"))
        first = text.splitlines()[0][:120] if text else ""
        return ToolResult(text if not is_error else f"Error: {text}", summary=first, detail=text[:4000],
                          detail_kind="text", is_error=is_error)


def load_config(root: Path, home: Path) -> dict[str, dict]:
    servers: dict[str, dict] = {}
    for path in (home / "mcp.json", root / ".mcp.json", root / ".nanocode" / "mcp.json"):
        try:
            servers.update(json.loads(path.read_text(encoding="utf-8")).get("mcpServers", {}))
        except (OSError, ValueError, AttributeError):
            continue
    return servers


def _schema(raw: dict | None) -> dict:
    schema = {k: v for k, v in (raw or {}).items() if k != "$schema"}
    schema.setdefault("type", "object")
    schema.setdefault("properties", {})
    return schema


class McpManager:
    """Starts every configured server and turns their tools into nanocode tools."""

    def __init__(self, root: Path, home: Path):
        self.root, self.home = root, home
        self.servers: dict[str, McpServer] = {}

    def start_all(self) -> dict[str, Tool]:
        tools: dict[str, Tool] = {}
        for name, cfg in load_config(self.root, self.home).items():
            server = McpServer(name, cfg.get("command", ""), cfg.get("args"), cfg.get("env"), self.root)
            self.servers[name] = server
            if cfg.get("disabled") or not cfg.get("command"):
                server.status = "disabled"
                continue
            try:
                server.start()
            except Exception as e:
                server.status, server.error = "failed", f"{e}{server._stderr_tail()}"
                server.close()
                server.status = "failed"
                continue
            for spec in server.tools:
                full = tool_name(name, spec["name"])
                description = (spec.get("description") or f"{spec['name']} (from the {name} MCP server)")[:1024]

                def fn(ctx, _server=server, _tool=spec["name"], **arguments):
                    return _server.call(_tool, arguments, ctx.max_output_chars, cancel=ctx.cancel)

                tools[full] = Tool(full, f"[MCP: {name}] {description}", _schema(spec.get("inputSchema")), fn)
        return tools

    def summary(self) -> list[dict]:
        return [{"name": s.name, "status": s.status, "tools": [t["name"] for t in s.tools], "error": s.error}
                for s in self.servers.values()]

    def close_all(self) -> None:
        for server in self.servers.values():
            server.close()
