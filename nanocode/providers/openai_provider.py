"""Any OpenAI-compatible Chat Completions API: OpenAI, OpenRouter, Gemini, Groq, LM Studio, vLLM...

The interesting part is streaming tool calls: they arrive as fragments ("index 0: name=read_",
"index 0: args={\"pa", ...) that we stitch back together by index.
"""

from __future__ import annotations

import json
from typing import Iterator

import openai

from .base import AssistantTurn, Event, Provider, ProviderError, Usage, looks_like_overflow, new_tool_id


def to_openai_messages(messages: list[dict]) -> list[dict]:
    out = []
    for m in messages:
        if m["role"] == "user":
            out.append({"role": "user", "content": m["content"]})
        elif m["role"] == "tool":
            out.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["content"] or "(no output)"})
        elif m.get("content") or m.get("tool_calls"):
            message = {"role": "assistant", "content": m.get("content") or ""}
            if m.get("tool_calls"):
                message["tool_calls"] = [{"id": c["id"], "type": "function",
                                          "function": {"name": c["name"], "arguments": json.dumps(c["args"])}}
                                         for c in m["tool_calls"]]
            out.append(message)
    return out


def _parse_args(raw: str) -> dict:
    try:
        args = json.loads(raw) if raw.strip() else {}
    except ValueError:
        return {"__invalid_json__": raw}
    return args if isinstance(args, dict) else {"__invalid_json__": raw}


class OpenAIProvider(Provider):
    kind = "openai"

    def __init__(self, model: str, *, name: str = "openai", base_url: str, api_key: str | None,
                 context_window: int = 128_000):
        self.name, self.model, self.context_window = name, model, context_window
        self.client = openai.OpenAI(base_url=base_url, api_key=api_key or "not-needed")
        self._stream_usage = True  # some servers reject stream_options; we turn it off if so

    def _open(self, kw: dict):
        try:
            return self.client.chat.completions.create(**kw)
        except openai.BadRequestError as e:
            if self._stream_usage and "stream_options" in str(e):
                self._stream_usage = False
                kw.pop("stream_options", None)
                return self._open(kw)
            raise

    def _stream(self, system: str, messages: list[dict], tools: list[dict]) -> Iterator[Event]:
        kw: dict = {"model": self.model, "stream": True,
                    "messages": [{"role": "system", "content": system}, *to_openai_messages(messages)]}
        if tools:
            kw["tools"] = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                             "parameters": t["parameters"]}} for t in tools]
        if self._stream_usage:
            kw["stream_options"] = {"include_usage": True}

        text: list[str] = []
        thinking: list[str] = []
        slots: dict[int, dict] = {}
        usage, finish = Usage(), None
        try:
            chunks = self._open(kw)
            self._active = chunks
            for chunk in chunks:
                if getattr(chunk, "usage", None):
                    u = chunk.usage
                    cached = getattr(getattr(u, "prompt_tokens_details", None), "cached_tokens", 0) or 0
                    usage = Usage(u.prompt_tokens or 0, u.completion_tokens or 0, cache_read_tokens=cached)
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                delta = choice.delta
                extra = getattr(delta, "model_extra", None) or {}
                reasoning = extra.get("reasoning_content") or extra.get("reasoning")  # OpenRouter, vLLM, LM Studio
                if isinstance(reasoning, str) and reasoning:
                    thinking.append(reasoning)
                    yield "thinking", reasoning
                if delta.content:
                    text.append(delta.content)
                    yield "text", delta.content
                for tc in delta.tool_calls or []:
                    slot = slots.setdefault(tc.index if tc.index is not None else len(slots),
                                            {"id": None, "name": "", "args": ""})
                    if tc.id:
                        slot["id"] = tc.id
                    if tc.function and tc.function.name and not slot["name"]:
                        slot["name"] = tc.function.name
                        yield "tool_start", tc.function.name
                    if tc.function and tc.function.arguments:
                        slot["args"] += tc.function.arguments
                if choice.finish_reason:
                    finish = choice.finish_reason
        except openai.AuthenticationError as e:
            raise ProviderError(f"{self.name} rejected your API key.") from e
        except openai.NotFoundError as e:
            raise ProviderError(f"Model '{self.model}' was not found on {self.name}. Use /model to pick another.") from e
        except openai.RateLimitError as e:
            raise ProviderError(f"Rate limited by {self.name}", retryable=True) from e
        except openai.BadRequestError as e:
            raise ProviderError(f"{self.name} rejected the request: {e.message}",
                                context_overflow=looks_like_overflow(str(e))) from e
        except openai.APIStatusError as e:
            raise ProviderError(f"{self.name} API error {e.status_code}: {e.message}",
                                retryable=e.status_code >= 500) from e
        except openai.APIConnectionError as e:
            raise ProviderError(f"Couldn't reach {self.name} (network error)", retryable=True) from e

        calls = [{"id": s["id"] or new_tool_id(), "name": s["name"], "args": _parse_args(s["args"])}
                 for _, s in sorted(slots.items()) if s["name"]]
        stop = {"length": "max_tokens", "content_filter": "refusal"}.get(finish or "", "tool_use" if calls else "end_turn")
        yield "done", AssistantTurn(text="".join(text), thinking="".join(thinking), tool_calls=calls,
                                    stop_reason=stop, usage=usage)
