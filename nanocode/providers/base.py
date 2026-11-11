"""The one interface every model provider implements.

nanocode stores the conversation in ONE simple format (plain dicts) and each provider
translates it to and from its own wire format:

    {"role": "user", "content": "fix the failing test"}
    {"role": "assistant", "content": "Let me look.",
     "tool_calls": [{"id": "call_1", "name": "read_file", "args": {"path": "app.py"}}]}
    {"role": "tool", "tool_call_id": "call_1", "name": "read_file", "content": "...", "is_error": False}

Assistant messages may also carry "thinking" (for display) and "raw": the provider's own
content blocks, so the same provider can replay them byte-for-byte on the next request.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Iterator

# A stream is a sequence of (kind, data) events:
#   ("text", str)        a chunk of the visible answer
#   ("thinking", str)    a chunk of reasoning (shown dimmed)
#   ("tool_start", str)  the model started writing a call to this tool
#   ("done", AssistantTurn)  always last
Event = tuple[str, Any]


class ProviderError(Exception):
    def __init__(self, message: str, *, retryable: bool = False, context_overflow: bool = False):
        super().__init__(message)
        self.retryable = retryable
        self.context_overflow = context_overflow


_OVERFLOW_HINTS = ("context length", "context window", "prompt is too long", "maximum context", "too many tokens", "exceeds the context")


def looks_like_overflow(message: str) -> bool:
    message = message.lower()
    return any(hint in message for hint in _OVERFLOW_HINTS)


def new_tool_id() -> str:
    return "call_" + uuid.uuid4().hex[:16]


@dataclass
class Usage:
    input_tokens: int = 0  # the whole prompt for this request, cached or not
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def add(self, other: "Usage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cache_write_tokens += other.cache_write_tokens


@dataclass
class AssistantTurn:
    text: str = ""
    thinking: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    stop_reason: str = "end_turn"  # end_turn | tool_use | max_tokens | refusal | pause_turn
    usage: Usage = field(default_factory=Usage)
    raw: Any = None  # provider-native content for exact replay
    raw_kind: str = ""  # which provider kind can replay `raw`
    note: str = ""  # extra detail, e.g. why a request was refused

    @property
    def is_empty(self) -> bool:
        return not self.text.strip() and not self.tool_calls

    def to_message(self) -> dict:
        message: dict = {"role": "assistant", "content": self.text, "tool_calls": self.tool_calls}
        if self.thinking:
            message["thinking"] = self.thinking
        if self.raw is not None:
            message["raw"], message["raw_kind"] = self.raw, self.raw_kind
        return message


class Provider:
    name = "base"  # provider id from config, e.g. "anthropic", "openrouter"
    kind = "base"  # wire format: anthropic | openai | ollama
    model = ""
    context_window = 128_000

    @property
    def label(self) -> str:
        return f"{self.name}:{self.model}"

    _aborted = False
    _active = None  # the live HTTP stream, so abort() can close it from another thread

    def stream(self, system: str, messages: list[dict], tools: list[dict]) -> Iterator[Event]:
        """Stream one model call. abort() (e.g. the desktop app's Stop button) ends it with KeyboardInterrupt."""
        self._aborted = False
        try:
            yield from self._stream(system, messages, tools)
        except Exception as e:
            if self._aborted:
                raise KeyboardInterrupt from e
            raise
        finally:
            self._active = None

    def abort(self) -> None:
        self._aborted = True
        active = self._active
        if active is not None:
            try:
                active.close()
            except Exception:
                pass

    def _stream(self, system: str, messages: list[dict], tools: list[dict]) -> Iterator[Event]:
        raise NotImplementedError
