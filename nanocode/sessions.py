"""Saved sessions: a conversation on disk, so you can resume it (and the desktop app can list it).

Sessions live in ~/.nanocode/sessions/<project>/<id>.json. Each file holds the conversation in
nanocode's own message format (provider-native blocks are already plain JSON), the todo list,
the per-turn checkpoints for rewind, token usage, and the "display" log the desktop app replays.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import uuid
from dataclasses import asdict
from pathlib import Path

from .config import HOME
from .providers import Usage

VERSION = 1
_SAVE_LOCK = threading.Lock()


def project_dir(root: Path) -> Path:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", root.name).strip("-")[:40] or "project"
    return HOME / "sessions" / f"{slug}-{hashlib.sha1(str(root).encode()).hexdigest()[:10]}"


def new_session_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]


def info(data: dict) -> dict:
    return {"id": data["id"], "title": data.get("title") or "New session", "created": data.get("created", 0),
            "updated": data.get("updated", 0), "model": data.get("model", ""), "turns": data.get("turn_count", 0)}


class SessionStore:
    def __init__(self, root: Path):
        self.dir = project_dir(root)

    def path(self, session_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", session_id):
            raise ValueError(f"Invalid session id: {session_id!r}")
        return self.dir / f"{session_id}.json"

    def save(self, data: dict) -> None:
        with _SAVE_LOCK:  # the desktop engine can save from two threads at once (turn end + shutdown)
            self.dir.mkdir(parents=True, exist_ok=True)
            target = self.path(data["id"])
            tmp = target.with_name(f".{target.stem}.{uuid.uuid4().hex[:8]}.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, default=str), encoding="utf-8")
            tmp.replace(target)  # atomic: a crash never leaves half a session on disk

    def load(self, session_id: str) -> dict:
        try:
            return json.loads(self.path(session_id).read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise ValueError(f"Session {session_id} not found.") from None

    def list(self) -> list[dict]:
        sessions = []
        for path in self.dir.glob("*.json"):
            try:
                sessions.append(info(json.loads(path.read_text(encoding="utf-8"))))
            except (OSError, ValueError, KeyError):
                continue
        return sorted(sessions, key=lambda s: s["updated"], reverse=True)

    def delete(self, session_id: str) -> None:
        self.path(session_id).unlink(missing_ok=True)

    def latest(self) -> dict | None:
        sessions = self.list()
        return self.load(sessions[0]["id"]) if sessions else None


def snapshot(agent, session_id: str, title: str, created: float, display: list[dict]) -> dict:
    return {
        "version": VERSION, "id": session_id, "title": title, "created": created, "updated": time.time(),
        "cwd": str(agent.root), "model": agent.provider.label, "mode": agent.permissions.mode,
        "turn_count": agent.turn_count, "messages": agent.messages, "todos": agent.ctx.todos,
        "checkpoints": agent.checkpoints, "display": display,
        "usage": {model: asdict(u) for model, u in agent.usage_by_model.items()},
    }


def restore(agent, data: dict) -> None:
    agent.reset()
    agent.messages = data.get("messages", [])
    agent.ctx.todos = data.get("todos", [])
    agent.checkpoints = data.get("checkpoints", [])
    agent.turn_count = data.get("turn_count", 0)
    agent.usage_by_model = {model: Usage(**u) for model, u in data.get("usage", {}).items()}
    agent._announced_mode = agent.permissions.mode  # the model was told about modes as they happened


def transcript_from_messages(messages: list[dict]) -> list[dict]:
    """Rebuild a display log from the raw conversation (for sessions saved by the terminal UI)."""
    from .ui import TOOL_LABELS, primary_arg

    events, turn = [], 0
    for m in messages:
        if m["role"] == "user":
            text = m.get("content") or ""
            if text.startswith(("<system-reminder>", "[Request interrupted", "This session continues")):
                continue
            turn += 1
            events.append({"event": "user", "turn_id": f"t{turn}", "text": text.split("\n\n<file path=")[0]})
        elif m["role"] == "assistant":
            if m.get("content"):
                events.append({"event": "assistant", "text": m["content"], "thinking": m.get("thinking", ""),
                               "thinking_seconds": 0})
            for c in m.get("tool_calls") or []:
                events.append({"event": "tool.call", "call_id": c["id"], "name": c["name"],
                               "label": TOOL_LABELS.get(c["name"], c["name"]), "arg": primary_arg(c["name"], c["args"]),
                               "args": c["args"]})
        elif m["role"] == "tool":
            first = (m.get("content") or "").strip().splitlines()[:1]
            events.append({"event": "tool.result", "call_id": m["tool_call_id"], "name": m.get("name", ""),
                           "summary": first[0][:160] if first else "", "detail": "", "detail_kind": "",
                           "is_error": bool(m.get("is_error"))})
    return events
