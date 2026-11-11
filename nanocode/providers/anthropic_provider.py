"""Claude, through the official Anthropic SDK (Messages API).

Three Claude-specific things worth knowing:
  * Prompt caching - we mark the request cacheable; because our system prompt and tools never
    change mid-session, every turn re-reads the cached prefix at a fraction of the price.
  * Thinking blocks are signed and tied to the exact conversation that produced them. We replay
    the raw content blocks verbatim, and send `prefix_mismatch_behavior: "drop_block"` so that if
    we ever trim history (micro-compaction) the API drops stale reasoning instead of erroring.
  * Refusal fallbacks - on models that support it, a declined request is retried server-side.
"""

from __future__ import annotations

from typing import Iterator

import anthropic

from .base import AssistantTurn, Event, Provider, ProviderError, Usage, looks_like_overflow

_MODERN = ("claude-opus-5", "claude-sonnet-5", "claude-haiku-5", "claude-fable-5", "claude-mythos-5")
_FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5", "claude-fable-5-1"}


def to_anthropic_messages(messages: list[dict]) -> list[dict]:
    out: list[dict] = []
    for m in messages:
        if m["role"] == "user":
            role, blocks = "user", [{"type": "text", "text": m["content"] or "(empty message)"}]
        elif m["role"] == "tool":
            role, blocks = "user", [{"type": "tool_result", "tool_use_id": m["tool_call_id"],
                                     "content": m["content"] or "(no output)", "is_error": bool(m.get("is_error"))}]
        else:
            role = "assistant"
            if m.get("raw") is not None and m.get("raw_kind") == "anthropic":
                blocks = list(m["raw"])  # exact replay: keeps signed thinking blocks intact
            else:  # this turn came from a different provider: rebuild it
                blocks = [{"type": "text", "text": m["content"]}] if m.get("content") else []
                blocks += [{"type": "tool_use", "id": c["id"], "name": c["name"], "input": c["args"]}
                           for c in m.get("tool_calls", [])]
            if not blocks:
                continue
        if out and out[-1]["role"] == role:
            out[-1]["content"].extend(blocks)  # tool results + a reminder become one user message
        else:
            out.append({"role": role, "content": blocks})
    return out


class AnthropicProvider(Provider):
    kind = "anthropic"

    def __init__(self, model: str, *, name: str = "anthropic", effort: str = "high",
                 api_key: str | None = None, base_url: str | None = None):
        self.name, self.model, self.effort = name, model, effort
        self.modern = model.startswith(_MODERN)
        self.context_window = 1_000_000 if self.modern else 200_000
        # With no api_key the SDK falls back to ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN / `ant auth login`.
        self.client = anthropic.Anthropic(api_key=api_key or None, base_url=base_url or None)

    def _request(self, system: str, messages: list[dict], tools: list[dict]) -> dict:
        kw: dict = {
            "model": self.model,
            "max_tokens": 64_000,
            "system": system,
            "messages": to_anthropic_messages(messages),
            "cache_control": {"type": "ephemeral"},  # automatic prompt caching
        }
        if tools:
            kw["tools"] = [{"name": t["name"], "description": t["description"], "input_schema": t["parameters"],
                            "eager_input_streaming": True} for t in tools]
        betas = []
        if self.modern:
            kw["thinking"] = {"type": "adaptive", "display": "summarized",
                              "block_binding": {"prefix_mismatch_behavior": "drop_block"}}
            kw["output_config"] = {"effort": self.effort}
            betas.append("thinking-binding-controls-2026-08-01")
        if self.model in _FALLBACK_MODELS:
            kw["fallbacks"] = "default"
            betas.append("server-side-fallback-2026-07-01")
        if betas:
            kw["betas"] = betas
        return kw

    def _stream(self, system: str, messages: list[dict], tools: list[dict]) -> Iterator[Event]:
        try:
            with self.client.beta.messages.stream(**self._request(system, messages, tools)) as stream:
                self._active = stream
                for event in stream:
                    if event.type == "content_block_start" and event.content_block.type == "tool_use":
                        yield "tool_start", event.content_block.name
                    elif event.type == "content_block_delta":
                        if event.delta.type == "text_delta":
                            yield "text", event.delta.text
                        elif event.delta.type == "thinking_delta":
                            yield "thinking", event.delta.thinking
                final = stream.get_final_message()
        except anthropic.AuthenticationError as e:
            raise ProviderError("Anthropic rejected your credentials. Check ANTHROPIC_API_KEY.") from e
        except anthropic.PermissionDeniedError as e:
            raise ProviderError(f"Permission denied by Anthropic: {e.message}") from e
        except anthropic.NotFoundError as e:
            raise ProviderError(f"Model '{self.model}' was not found. Use /model to pick another.") from e
        except anthropic.RateLimitError as e:
            raise ProviderError("Rate limited by Anthropic", retryable=True) from e
        except anthropic.BadRequestError as e:
            raise ProviderError(f"Anthropic rejected the request: {e.message}",
                                context_overflow=looks_like_overflow(e.message)) from e
        except anthropic.APIStatusError as e:
            raise ProviderError(f"Anthropic API error {e.status_code}: {e.message}",
                                retryable=e.status_code >= 500) from e
        except anthropic.APIConnectionError as e:
            raise ProviderError("Couldn't reach the Anthropic API (network error)", retryable=True) from e
        except anthropic.AnthropicError as e:  # e.g. no credentials configured at all
            raise ProviderError(f"Anthropic client error: {e}") from e
        except ValueError as e:  # streamed tool-call JSON that couldn't be parsed at all
            raise ProviderError(f"The model produced malformed tool-call JSON ({e})", retryable=True) from e
        yield "done", self._to_turn(final)

    def _to_turn(self, message) -> AssistantTurn:
        blocks = list(message.content)
        # After a mid-answer refusal fallback, blocks before the last `fallback` marker came from the
        # declined attempt: keep their text, drop their thinking/tool calls (as the API requires).
        cut = max((i for i, b in enumerate(blocks) if b.type == "fallback"), default=-1)
        raw = [b for i, b in enumerate(blocks) if i >= cut or b.type == "text"]

        text = "".join(b.text for b in raw if b.type == "text")
        thinking = "\n\n".join(b.thinking for b in raw if b.type == "thinking" and getattr(b, "thinking", ""))
        calls = [{"id": b.id, "name": b.name, "args": b.input if isinstance(b.input, dict) else {}}
                 for b in raw if b.type == "tool_use"]

        note = ""
        if message.stop_reason == "refusal":
            stop = "refusal"
            details = getattr(message, "stop_details", None)
            note = (getattr(details, "explanation", None) or getattr(details, "category", None) or "") if details else ""
        elif message.stop_reason == "max_tokens":
            stop = "max_tokens"
        elif calls:
            stop = "tool_use"
        elif message.stop_reason == "pause_turn":
            stop = "pause_turn"
        else:
            stop = "end_turn"

        u = message.usage
        cache_read = getattr(u, "cache_read_input_tokens", 0) or 0
        cache_write = getattr(u, "cache_creation_input_tokens", 0) or 0
        usage = Usage(input_tokens=(u.input_tokens or 0) + cache_read + cache_write, output_tokens=u.output_tokens or 0,
                      cache_read_tokens=cache_read, cache_write_tokens=cache_write)
        return AssistantTurn(text=text, thinking=thinking, tool_calls=calls, stop_reason=stop, usage=usage,
                             raw=[b.to_dict(mode="json", exclude_none=True) for b in raw], raw_kind="anthropic", note=note)
