"""`nanocode serve`: the engine behind the desktop app.

It speaks the protocol in docs/PROTOCOL.md - newline-delimited JSON on stdin/stdout: one response
per request, and everything that happens during a turn streams back as events. The agent itself
is exactly the one the terminal UI drives; only the UI object is different (ServerUI turns every
UI call into an event). The split - one agent server, any number of thin clients - follows
OpenCode's architecture (anomalyco/opencode, MIT).
"""

from __future__ import annotations

import argparse
import contextlib
import itertools
import json
import os
import sys
import threading
import time
import traceback
from pathlib import Path

from . import __version__
from .agent import Agent
from .commands import _cost, custom_commands, expand_mentions, remember, run_shell
from .config import (EFFORTS, HOME, MODEL_PRESETS, MODES, Settings, detect_default_model, load_dotenv,
                     load_settings, ollama_models)
from .context import COMPACT_AT, MICROCOMPACT_AT, estimate_tokens, todos_text
from .mcp import McpManager
from .permissions import describe_request
from .prompts import INIT_PROMPT
from .providers import Provider, ProviderError
from .sessions import SessionStore, info, new_session_id, restore, snapshot, transcript_from_messages
from .tools import walk_files
from .ui import fmt_tokens, primary_arg, tool_label

COMMANDS = [
    ("compact", "Summarize the conversation to free up context"),
    ("init", "Analyze this project and write an AGENTS.md memory file"),
    ("model", "Switch model, e.g. /model ollama:qwen3:8b"),
    ("mode", "Set the mode: default, accept-edits, plan"),
    ("effort", "Claude reasoning effort: low, medium, high, xhigh, max"),
    ("think", "Reasoning for local models: /think on | off"),
    ("context", "How full the context window is"),
    ("cost", "Token usage and estimated cost"),
    ("todos", "Show the todo list"),
    ("memory", "Which memory files are loaded"),
    ("help", "Commands and shortcuts"),
]


class NoModel(Provider):
    """Lets the app start (and show the model picker) when no model is reachable yet."""

    name, kind, model, context_window = "none", "none", "no model", 32_768

    def _stream(self, system, messages, tools):
        raise ProviderError("No model is configured. Pick one from the model menu, or add an API key in Settings.")
        yield  # pragma: no cover


class _StreamView:
    def __init__(self, server: "Server", parent):
        self.server, self.parent = server, parent
        self.text = self.thinking = ""
        self.think_start = self.think_end = 0.0

    def update(self, kind: str, data) -> None:
        if self.server.agent.ctx.cancel.is_set():
            raise KeyboardInterrupt
        if kind == "text":
            self.text += data
            self.server.emit("text", delta=data, parent=self.parent)
        elif kind == "thinking":
            self.thinking += data
            self.think_start = self.think_start or time.monotonic()
            self.think_end = time.monotonic()
            self.server.emit("thinking", delta=data, parent=self.parent)
        elif kind == "tool_start":
            self.server.emit("tool.preparing", name=data, parent=self.parent)


class ServerUI:
    """Everything the Agent asks a UI to do, turned into protocol events."""

    interactive = True

    def __init__(self, server: "Server"):
        self.server = server
        self.show_thinking = True
        self._waiting: dict[str, list] = {}  # request_id -> [Event, response]
        self._last_call_id = None
        self._ids = itertools.count(1)

    # streaming
    @contextlib.contextmanager
    def stream_view(self, parent=None):
        view = _StreamView(self.server, parent)
        self.server.emit("stream.start", parent=parent)
        try:
            yield view
        finally:
            seconds = round(view.think_end - view.think_start) if view.thinking else 0
            self.server.emit("assistant", log=True, text=view.text, thinking=view.thinking,
                             thinking_seconds=seconds, parent=parent)
            self.server.emit_status()

    @contextlib.contextmanager
    def spinner(self, label: str):
        yield

    @contextlib.contextmanager
    def tool_spinner(self, name: str, args: dict):
        yield

    # tools
    def tool_call(self, name, args, call_id=None, parent=None):
        call_id = call_id or f"u{next(self._ids)}"
        self._last_call_id = call_id
        self.server.emit("tool.call", log=True, call_id=call_id, name=name, label=tool_label(name),
                         arg=primary_arg(name, args), args=args, parent=parent)

    def tool_result(self, name, args, result, todos=None, call_id=None, parent=None):
        self.server.emit("tool.result", log=True, call_id=call_id or self._last_call_id, name=name,
                         summary=result.summary or ("Done" if not result.is_error else result.output[:200]),
                         detail=result.detail, detail_kind=result.detail_kind, is_error=result.is_error, parent=parent)
        if result.detail_kind == "todos":
            self.server.emit("todos", log=True, todos=self.server.agent.ctx.todos)

    # questions
    def _ask(self, event: str, **fields) -> dict:
        request_id = f"r{next(self._ids)}"
        slot = [threading.Event(), None]
        self._waiting[request_id] = slot
        self.server.emit(event, request_id=request_id, **fields)
        try:
            while not slot[0].wait(0.1):
                if self.server.agent.ctx.cancel.is_set():
                    raise KeyboardInterrupt
            return slot[1]
        finally:
            self._waiting.pop(request_id, None)

    def respond(self, request_id: str, payload: dict) -> None:
        slot = self._waiting.get(request_id)
        if slot is None:
            raise ValueError("That request is no longer waiting for an answer.")
        slot[1] = payload
        slot[0].set()

    def ask_permission(self, name, args, preview, permissions, call_id=None):
        answer = self._ask("permission.request", call_id=call_id or self._last_call_id, name=name,
                           **describe_request(name, args, preview))
        return answer.get("answer", "no"), (answer.get("feedback") or "").strip()

    def ask_plan_approval(self, plan):
        answer = self._ask("plan.request", plan=plan)
        return answer.get("choice", "no"), (answer.get("feedback") or "").strip()

    # messages
    def info(self, message):
        self.server.emit("info", log=True, message=message)

    def warn(self, message):
        self.server.emit("warn", log=True, message=message)

    def error(self, message):
        self.server.emit("error", log=True, message=message)

    def interrupted(self):
        self.server.emit("interrupted", log=True)

    def compacted(self, before, after, limit, summary):
        self.server.emit("compacted", log=True, before=before, after=after, limit=limit, summary=summary)


class Server:
    def __init__(self, root: Path, settings: Settings, out=None, inp=None, provider: Provider | None = None,
                 start_mcp: bool = True):
        self.root, self.settings = root, settings
        self.out, self.inp = out or sys.stdout, inp or sys.stdin
        self._write_lock = threading.Lock()
        self._busy = False
        self._quit = False
        self.ui = ServerUI(self)
        if provider is None and settings.model:
            try:
                self.agent = Agent(settings, self.ui, root)
            except ProviderError as e:
                self._startup_warning = str(e)
                self.agent = Agent(settings, self.ui, root, provider=NoModel())
            else:
                self._startup_warning = ""
        else:
            self._startup_warning = "" if provider else "No model found."
            self.agent = Agent(settings, self.ui, root, provider=provider or NoModel())
        self.mcp = McpManager(root, HOME)
        if start_mcp:
            self.agent.add_tools(self.mcp.start_all())
        self.store = SessionStore(root)
        self._new_session_state()
        self._files: list[str] = []
        self._files_at = 0.0

    # ------------------------------------------------------------------ plumbing

    def emit(self, event: str, log: bool = False, **fields) -> None:
        payload = {"event": event, **{k: v for k, v in fields.items() if v is not None or k != "parent"}}
        if log:
            self.display.append(payload)
            self._dirty = True  # something worth saving happened
        self._write(payload)

    def _write(self, obj: dict) -> None:
        line = json.dumps(obj, ensure_ascii=False, default=str)
        with self._write_lock:
            self.out.write(line + "\n")
            self.out.flush()

    def status(self) -> dict:
        return {"mode": self.agent.permissions.mode, "model": self.agent.provider.label, "busy": self._busy,
                "context_used": self.agent.context_used(), "context_limit": self.agent.context_limit()}

    def emit_status(self) -> None:
        self.emit("status", **self.status())

    def serve_forever(self) -> None:
        self.emit("ready", version=__version__)
        for line in self.inp:
            line = line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except ValueError:
                self._write({"id": None, "error": {"message": "Invalid JSON."}})
                continue
            self.handle(request)
            if self._quit:
                break
        self.close()

    def handle(self, request: dict) -> None:
        request_id, method = request.get("id"), str(request.get("method", ""))
        params = request.get("params") or {}
        handler = getattr(self, "m_" + method.replace(".", "_"), None)
        if handler is None:
            self._write({"id": request_id, "error": {"message": f"Unknown method {method!r}."}})
            return
        try:
            result = handler(**params)
        except Exception as e:  # report any failure to the client, never crash the engine
            if not isinstance(e, (ValueError, ProviderError, RuntimeError, TypeError)):
                traceback.print_exc(file=sys.stderr)
            self._write({"id": request_id, "error": {"message": str(e) or type(e).__name__}})
            return
        self._write({"id": request_id, "result": result or {}})

    def _start(self, job, *args) -> None:
        if self._busy:
            raise RuntimeError("nanocode is still working. Stop it first, or wait for it to finish.")
        self._busy = True
        self.agent.ctx.cancel.clear()
        threading.Thread(target=self._run, args=(job, args), daemon=True).start()

    def _run(self, job, args) -> None:
        self.emit_status()
        try:
            job(*args)
        except KeyboardInterrupt:
            self.ui.interrupted()
        except Exception as e:
            traceback.print_exc(file=sys.stderr)
            self.ui.error(f"Internal error: {type(e).__name__}: {e}")
        finally:
            self._busy = False
            self.save()
            self.emit("session", session=self.session_info())
            self.emit_status()

    def _require_idle(self) -> None:
        if self._busy:
            raise RuntimeError("nanocode is still working. Stop it first, or wait for it to finish.")

    def close(self) -> None:
        self.agent.ctx.cancel.set()
        self.save()
        self.mcp.close_all()

    # ------------------------------------------------------------------ sessions

    def _new_session_state(self) -> None:
        self.session_id, self.session_title, self.session_created = new_session_id(), "", time.time()
        self.display: list[dict] = []
        self._dirty = False
        self.turn_seq = 0

    def session_info(self) -> dict:
        return {"id": self.session_id, "title": self.session_title or "New session", "created": self.session_created,
                "updated": time.time(), "model": self.agent.provider.label, "turns": self.turn_seq}

    def save(self) -> None:
        """Only saves when something changed, so merely switching sessions doesn't reorder the sidebar."""
        if not self._dirty or (not self.agent.messages and not self.display):
            return
        self._dirty = False
        data = snapshot(self.agent, self.session_id, self.session_title, self.session_created, self.display)
        data["turn_count"] = self.turn_seq
        try:
            self.store.save(data)
        except OSError as e:
            print(f"could not save session: {e}", file=sys.stderr)

    # ------------------------------------------------------------------ methods: lifecycle

    def m_initialize(self) -> dict:
        if self._startup_warning:
            self.ui.warn(self._startup_warning)
        return {"version": __version__, "cwd": str(self.root), "status": self.status(),
                "effort": self.settings.effort, "think": self.settings.think, "session": self.session_info(),
                "sessions": self.store.list(), "commands": self._commands(), "mcp": self.mcp.summary(),
                "keys": self._keys()}

    def m_shutdown(self) -> dict:
        self._quit = True
        return {}

    def _keys(self) -> dict:
        envs = {cfg.get("api_key_env") for cfg in self.settings.providers.values() if cfg.get("api_key_env")}
        return {env: bool(os.environ.get(env)) for env in sorted(envs)}

    def _commands(self) -> list[dict]:
        items = [{"name": n, "description": d} for n, d in COMMANDS]
        items += [{"name": n, "description": "Custom command"} for n in custom_commands(self.agent)
                  if n not in dict(COMMANDS)]
        return items

    # ------------------------------------------------------------------ methods: turns

    def m_prompt(self, text: str) -> dict:
        text = text.strip()
        if not text:
            raise ValueError("Type a message first.")
        self._require_idle()
        self.turn_seq += 1
        turn_id = f"t{self.turn_seq}"
        if not self.session_title and not text.startswith(("/", "!", "#")):
            self.session_title = " ".join(text.split())[:60]
        self._start(self._prompt_job, text, turn_id)
        return {"turn_id": turn_id}

    def _prompt_job(self, text: str, turn_id: str) -> None:
        self.emit("user", log=True, turn_id=turn_id, text=text)
        self.emit("session", session=self.session_info())  # the title is known now; don't make the client guess
        checkpoint = None
        try:
            if text.startswith("/"):
                checkpoint = self._slash(text, turn_id)
            elif text.startswith("!"):
                run_shell(self.agent, self.ui, text[1:].strip())
            elif text.startswith("#") and not text.startswith("##"):
                remember(self.agent, self.ui, text[1:].strip())
            else:
                checkpoint = self._agent_turn(expand_mentions(self.agent, self.ui, text), text, turn_id)
        finally:
            self.save()  # before turn.end, so a client refreshing its session list on turn.end sees this turn
            self.emit("turn.end", turn_id=turn_id, checkpoint=_public(checkpoint, self.root))

    def _agent_turn(self, content: str, label: str, turn_id: str) -> dict | None:
        self.agent.run_turn(content, label=label, turn_id=turn_id)
        cp = self.agent.last_checkpoint
        return cp if cp and cp["turn_id"] == turn_id else None

    def _slash(self, text: str, turn_id: str) -> dict | None:
        name, _, arg = text[1:].partition(" ")
        arg = arg.strip()
        if name == "compact":
            self.agent.compact(arg, auto=False)
        elif name == "init":
            return self._agent_turn(INIT_PROMPT, "/init", turn_id)
        elif name == "model":
            if arg:
                try:
                    self._set_model(arg)
                    self.ui.info(f"Now using {self.agent.provider.label}")
                except ProviderError as e:
                    self.ui.error(str(e))
            else:
                self.ui.info(f"Current model: {self.agent.provider.label}. Pick another from the model menu.")
        elif name == "mode":
            self.m_mode_set(arg) if arg else self.ui.info(f"Mode: {self.agent.permissions.mode}")
        elif name == "effort":
            self.m_effort_set(arg) if arg else self.ui.info(f"Effort: {self.settings.effort}")
        elif name == "think":
            self.m_think_set(arg == "on") if arg in ("on", "off") else self.ui.info("Use /think on or /think off.")
        elif name == "context":
            c = self.m_context_get()
            self.ui.info(f"Context: {fmt_tokens(c['used'])} / {fmt_tokens(c['limit'])} tokens · system "
                         f"{fmt_tokens(c['system'])} · tools {fmt_tokens(c['tools'])} · messages {fmt_tokens(c['messages'])}")
        elif name == "cost":
            rows = self.m_cost_get()["rows"]
            self.ui.info(" · ".join(f"{r['model']}: {r['cost']}" for r in rows) or "No model calls yet.")
        elif name == "todos":
            self.ui.info(todos_text(self.agent.ctx.todos) or "No todos yet.")
        elif name == "memory":
            names = ", ".join(str(p) for p, _ in self.agent.memory)
            self.ui.info(f"Memory: {names}" if names else "No memory files. Run /init to create AGENTS.md.")
        elif name == "help":
            self.ui.info("Commands: " + ", ".join("/" + c["name"] for c in self._commands())
                         + " · !cmd runs a shell command · # saves a note to AGENTS.md · @file attaches a file")
        elif name in custom_commands(self.agent):
            template = custom_commands(self.agent)[name].read_text(encoding="utf-8")
            prompt = template.replace("$ARGUMENTS", arg) if "$ARGUMENTS" in template else (template + "\n\n" + arg).strip()
            return self._agent_turn(expand_mentions(self.agent, self.ui, prompt), text, turn_id)
        else:
            self.ui.error(f"Unknown command /{name}.")
        return None

    def m_interrupt(self) -> dict:
        self.agent.ctx.cancel.set()
        self.agent.provider.abort()
        return {}

    def m_permission_respond(self, request_id: str, answer: str, feedback: str = "") -> dict:
        if answer not in ("yes", "always", "no"):
            raise ValueError("answer must be yes, always or no")
        self.ui.respond(request_id, {"answer": answer, "feedback": feedback})
        return {}

    def m_plan_respond(self, request_id: str, choice: str, feedback: str = "") -> dict:
        if choice not in ("accept-edits", "default", "no"):
            raise ValueError("choice must be accept-edits, default or no")
        self.ui.respond(request_id, {"choice": choice, "feedback": feedback})
        return {}

    def m_compact(self, instructions: str = "") -> dict:
        self._start(lambda: self.agent.compact(instructions, auto=False))
        return {}

    # ------------------------------------------------------------------ methods: settings

    def m_mode_set(self, mode: str) -> dict:
        if mode not in MODES or (mode == "yolo" and not self.settings.allow_yolo_cycle):
            raise ValueError("Mode must be default, accept-edits or plan." + (
                " (yolo needs --yolo)" if mode == "yolo" else ""))
        self.agent.permissions.mode = mode
        self.emit_status()
        return {"status": self.status()}

    def m_mode_cycle(self) -> dict:
        self.agent.permissions.cycle(allow_yolo=self.settings.allow_yolo_cycle)
        self.emit_status()
        return {"status": self.status()}

    def m_models_list(self) -> dict:
        options = []
        for spec, description in MODEL_PRESETS:
            provider = spec.split(":", 1)[0]
            key_env = self.settings.providers[provider].get("api_key_env")
            ready = not key_env or bool(os.environ.get(key_env))
            name, _, detail = description.partition(" - ")  # "Claude Opus 5.5 - most capable (...)"
            label = name if name.startswith("Claude") else spec.split(":", 1)[1]
            options.append({"spec": spec, "label": label, "detail": detail or description,
                            "group": "Anthropic" if provider == "anthropic" else "OpenAI-compatible",
                            "available": ready, **({} if ready else {"needs": key_env})})
        for m in ollama_models(self.settings.providers["ollama"]["base_url"]):
            if m["tools"]:
                tags = "local · free" + (" · thinking" if m["thinking"] else "")
                options.append({"spec": f"ollama:{m['name']}", "label": m["name"],
                                "detail": f"{m['size']} · {tags}".strip(" ·"), "group": "Local (Ollama)",
                                "available": True})
        return {"current": self.agent.provider.label, "options": options}

    def m_model_set(self, spec: str) -> dict:
        self._require_idle()  # never swap the model under a running turn
        return self._set_model(spec)

    def _set_model(self, spec: str) -> dict:
        self.agent.set_model(spec)  # raises ProviderError with a readable message
        self.settings.model = self.agent.provider.label
        self.emit_status()
        return {"status": self.status()}

    def m_effort_set(self, effort: str) -> dict:
        if effort not in EFFORTS:
            raise ValueError(f"Effort must be one of: {', '.join(EFFORTS)}")
        self.settings.effort = effort
        if hasattr(self.agent.provider, "effort"):
            self.agent.provider.effort = effort
        return {}

    def m_think_set(self, on: bool) -> dict:
        self.settings.think = self.settings.show_thinking = bool(on)
        if hasattr(self.agent.provider, "think"):
            self.agent.provider.think = bool(on)
        return {}

    # ------------------------------------------------------------------ methods: inspection

    def m_context_get(self) -> dict:
        agent = self.agent
        used, system, tools = agent.context_used(), estimate_tokens(agent.system), estimate_tokens(json.dumps(agent.tools))
        return {"used": used, "limit": agent.context_limit(), "window": agent.provider.context_window,
                "system": system, "tools": tools, "messages": max(0, used - system - tools),
                "microcompact_at": MICROCOMPACT_AT, "compact_at": COMPACT_AT}

    def m_cost_get(self) -> dict:
        return {"rows": [{"model": spec, "input": u.input_tokens, "cached": u.cache_read_tokens,
                          "output": u.output_tokens, "cost": _cost(spec, u)}
                         for spec, u in self.agent.usage_by_model.items()]}

    def m_todos_get(self) -> dict:
        return {"todos": self.agent.ctx.todos}

    def m_mcp_list(self) -> dict:
        return {"servers": self.mcp.summary()}

    def m_files_search(self, query: str = "", limit: int = 50) -> dict:
        if time.monotonic() - self._files_at > 20:
            self._files = []
            for i, path in enumerate(walk_files(self.root)):
                if i >= 10_000:
                    break
                self._files.append(path.relative_to(self.root).as_posix())
            self._files_at = time.monotonic()
        needle = query.lower()
        hits = [p for p in self._files if needle in p.lower()]
        hits.sort(key=lambda p: (not p.lower().split("/")[-1].startswith(needle), len(p)))
        return {"files": hits[: max(1, int(limit))]}

    # ------------------------------------------------------------------ methods: sessions & checkpoints

    def m_session_list(self) -> dict:
        return {"sessions": self.store.list()}

    def m_session_new(self) -> dict:
        self._require_idle()
        self.save()
        self.agent.reset()
        self._new_session_state()
        self.emit_status()
        return {"session": self.session_info()}

    def m_session_load(self, session_id: str) -> dict:
        self._require_idle()
        data = self.store.load(session_id)
        self.save()
        restore(self.agent, data)
        if data.get("model") and data["model"] != self.agent.provider.label:
            try:
                self.agent.set_model(data["model"])
            except ProviderError as e:
                self.ui.warn(f"Couldn't switch back to {data['model']} ({e}). Using {self.agent.provider.label}.")
        self.session_id, self.session_title = data["id"], data.get("title", "")
        self.session_created, self.turn_seq = data.get("created", time.time()), data.get("turn_count", 0)
        self.display = data.get("display") or transcript_from_messages(self.agent.messages)
        self._dirty = False  # just loaded: nothing new to save until something happens
        self.emit_status()
        return {"session": self.session_info(), "transcript": self.display, "todos": self.agent.ctx.todos,
                "status": self.status()}

    def m_session_rename(self, session_id: str, title: str) -> dict:
        if session_id == self.session_id:
            self.session_title = title.strip()[:80]
            self._dirty = True
            self.save()
            return {"session": self.session_info()}
        data = self.store.load(session_id)
        data["title"] = title.strip()[:80]
        self.store.save(data)
        return {"session": info(data)}

    def m_session_delete(self, session_id: str) -> dict:
        if session_id == self.session_id:
            raise ValueError("You can't delete the session that's open. Switch to another one first.")
        self.store.delete(session_id)
        return {}

    def m_checkpoint_list(self) -> dict:
        return {"checkpoints": [_public(c, self.root) for c in self.agent.checkpoints]}

    def m_checkpoint_restore(self, turn_id: str, conversation: bool = False) -> dict:
        self._require_idle()
        restored = self.agent.restore_checkpoint(turn_id, conversation=conversation)
        result: dict = {"restored": restored}
        if conversation:
            cut = next((i for i, e in enumerate(self.display) if e.get("event") == "user" and e.get("turn_id") == turn_id),
                       len(self.display))
            self.display = self.display[:cut]
        self.ui.info(f"Rewound to before {turn_id}: restored {len(restored)} file{'' if len(restored) == 1 else 's'}"
                     + (" and the conversation" if conversation else ""))  # logged, so it survives a reload
        if conversation:
            result["transcript"] = list(self.display)
        self.save()
        self.emit_status()
        result["status"] = self.status()
        return result


def _public(checkpoint: dict | None, root: Path) -> dict | None:
    """A checkpoint as the client sees it: which files, not their saved contents."""
    if not checkpoint:
        return None
    files = []
    for path in checkpoint["files"]:
        try:
            files.append(Path(path).relative_to(root).as_posix())
        except ValueError:
            files.append(path)
    return {"turn_id": checkpoint["turn_id"], "prompt": checkpoint["prompt"], "created": checkpoint["created"],
            "files": sorted(files)}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="nanocode serve", description="Run the nanocode engine for the desktop app.")
    parser.add_argument("--cwd", help="project folder (default: current directory)")
    parser.add_argument("--model", help="provider:model")
    parser.add_argument("--mode", choices=[m for m in MODES if m != "yolo"])
    parser.add_argument("--no-mcp", action="store_true", help="don't start MCP servers")
    args = parser.parse_args(argv)
    if args.cwd:
        os.chdir(args.cwd)
    root = Path.cwd().resolve()
    load_dotenv(root / ".env", HOME / ".env")
    settings = load_settings({"model": args.model, "mode": args.mode})
    if settings.mode == "yolo":
        settings.mode = "default"  # the app never starts in yolo
    if not settings.model:
        settings.model = detect_default_model(settings)
    protocol_out = sys.stdout
    sys.stdout = sys.stderr  # anything that prints by accident must not corrupt the protocol stream
    server = Server(root, settings, out=protocol_out, inp=sys.stdin, start_mcp=not args.no_mcp)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.close()
