"""Local models through Ollama's native API - free, private, offline.

Why the native /api/chat instead of Ollama's OpenAI-compatible endpoint? Only the native API
lets us set `num_ctx` per request. Ollama's default context window is small, and when a
conversation outgrows it Ollama silently drops the oldest tokens - including your system
prompt. The agent doesn't crash; it just quietly "forgets" how to be an agent.
"""

from __future__ import annotations

import json
from typing import Iterator

import httpx

from .base import AssistantTurn, Event, Provider, ProviderError, Usage, looks_like_overflow, new_tool_id


def to_ollama_messages(messages: list[dict]) -> list[dict]:
    out = []
    for m in messages:
        if m["role"] == "user":
            out.append({"role": "user", "content": m["content"]})
        elif m["role"] == "tool":
            out.append({"role": "tool", "content": m["content"] or "(no output)", "tool_name": m.get("name", "")})
        elif m.get("content") or m.get("tool_calls"):
            message = {"role": "assistant", "content": m.get("content") or ""}
            if m.get("tool_calls"):
                message["tool_calls"] = [{"id": c["id"], "function": {"name": c["name"], "arguments": c["args"]}}
                                         for c in m["tool_calls"]]
            out.append(message)
    return out


class OllamaProvider(Provider):
    kind = "ollama"

    def __init__(self, model: str, *, name: str = "ollama", base_url: str = "http://localhost:11434",
                 num_ctx: int = 32768, think: bool = True):
        self.name, self.model, self.think = name, model, think
        self.base_url = base_url.rstrip("/")
        self.http = httpx.Client(base_url=self.base_url, timeout=httpx.Timeout(600.0, connect=5.0))
        info = self._show()
        caps = info.get("capabilities") or []
        self.supports_thinking = "thinking" in caps
        if caps and "tools" not in caps:
            raise ProviderError(f"'{model}' doesn't support tool calling, so it can't drive an agent. "
                                "Try a tool-capable model, e.g. `ollama pull qwen3:8b`.")
        model_max = next((v for k, v in (info.get("model_info") or {}).items() if k.endswith(".context_length")), None)
        self.context_window = min(num_ctx, int(model_max)) if model_max else num_ctx

    def _show(self) -> dict:
        try:
            response = self.http.post("/api/show", json={"model": self.model})
        except httpx.HTTPError as e:
            raise ProviderError(f"Can't reach Ollama at {self.base_url}. Is it running? Start it with `ollama serve`.") from e
        if response.status_code == 404:
            raise ProviderError(f"Model '{self.model}' isn't installed. Run `ollama pull {self.model}`.")
        if response.status_code != 200:
            raise ProviderError(f"Ollama error {response.status_code}: {response.text[:300]}")
        return response.json()

    def _stream(self, system: str, messages: list[dict], tools: list[dict]) -> Iterator[Event]:
        body: dict = {
            "model": self.model,
            "stream": True,
            "messages": [{"role": "system", "content": system}, *to_ollama_messages(messages)],
            "options": {"num_ctx": self.context_window},  # the line that makes local agents work
        }
        if tools:
            body["tools"] = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                               "parameters": t["parameters"]}} for t in tools]
        if self.supports_thinking:
            body["think"] = self.think

        text: list[str] = []
        thinking: list[str] = []
        calls: list[dict] = []
        usage, done_reason = Usage(), "stop"
        try:
            with self.http.stream("POST", "/api/chat", json=body) as response:
                self._active = response
                if response.status_code != 200:
                    response.read()
                    try:
                        detail = response.json().get("error", response.text)
                    except ValueError:
                        detail = response.text
                    raise ProviderError(f"Ollama error: {detail}", context_overflow=looks_like_overflow(detail))
                for line in response.iter_lines():
                    if not line.strip():
                        continue
                    chunk = json.loads(line)
                    if chunk.get("error"):
                        raise ProviderError(f"Ollama error: {chunk['error']}")
                    message = chunk.get("message") or {}
                    if message.get("thinking"):
                        thinking.append(message["thinking"])
                        yield "thinking", message["thinking"]
                    if message.get("content"):
                        text.append(message["content"])
                        yield "text", message["content"]
                    for tc in message.get("tool_calls") or []:  # Ollama sends each call whole
                        fn = tc.get("function") or {}
                        args = fn.get("arguments") or {}
                        if isinstance(args, str):
                            try:
                                args = json.loads(args)
                            except ValueError:
                                args = {"__invalid_json__": args}
                        calls.append({"id": tc.get("id") or new_tool_id(), "name": fn.get("name", ""), "args": args})
                        yield "tool_start", fn.get("name", "")
                    if chunk.get("done"):  # prompt_eval_count is the whole prompt, cached part included
                        usage = Usage(input_tokens=chunk.get("prompt_eval_count") or 0,
                                      output_tokens=chunk.get("eval_count") or 0,
                                      cache_read_tokens=chunk.get("prompt_eval_cached_count") or 0)
                        done_reason = chunk.get("done_reason") or "stop"
        except httpx.HTTPError as e:
            raise ProviderError(f"Lost the connection to Ollama: {e}", retryable=True) from e

        stop = "max_tokens" if done_reason == "length" else ("tool_use" if calls else "end_turn")
        yield "done", AssistantTurn(text="".join(text), thinking="".join(thinking), tool_calls=calls,
                                    stop_reason=stop, usage=usage)
