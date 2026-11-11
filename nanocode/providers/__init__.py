"""Model providers. `make_provider("ollama:qwen3:8b", settings)` gives you something that can stream."""

from __future__ import annotations

import os

from ..config import Settings, parse_model_spec
from .base import AssistantTurn, Event, Provider, ProviderError, Usage, new_tool_id

__all__ = ["AssistantTurn", "Event", "Provider", "ProviderError", "Usage", "make_provider", "new_tool_id"]


def make_provider(spec: str, settings: Settings) -> Provider:
    try:
        name, model = parse_model_spec(spec, settings.providers)
    except ValueError as e:
        raise ProviderError(str(e)) from e
    cfg = settings.providers[name]
    key_env = cfg.get("api_key_env")
    api_key = os.environ.get(key_env) if key_env else None

    # SDKs are imported lazily, so you only need the ones you actually use.
    if cfg["kind"] == "anthropic":
        from .anthropic_provider import AnthropicProvider

        return AnthropicProvider(model, name=name, effort=settings.effort, api_key=api_key, base_url=cfg.get("base_url"))
    if cfg["kind"] == "openai":
        from .openai_provider import OpenAIProvider

        if key_env and not api_key:
            raise ProviderError(f"'{name}' needs an API key: set {key_env} in your shell or in a .env file.")
        return OpenAIProvider(model, name=name, base_url=cfg["base_url"], api_key=api_key,
                              context_window=cfg.get("context_window", 128_000))
    if cfg["kind"] == "ollama":
        from .ollama_provider import OllamaProvider

        return OllamaProvider(model, name=name, base_url=cfg["base_url"], num_ctx=settings.num_ctx, think=settings.think)
    raise ProviderError(f"Unknown provider kind '{cfg['kind']}' for '{name}'.")
