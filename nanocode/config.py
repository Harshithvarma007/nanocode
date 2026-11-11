"""Settings, providers and model selection.

Precedence (highest wins): CLI flags > environment variables > ~/.nanocode/config.json > defaults.
"""

from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

import httpx

HOME = Path(os.environ.get("NANOCODE_HOME", Path.home() / ".nanocode"))

# A provider is somewhere models live. Its "kind" is the wire format we speak to it:
#   anthropic -> the Anthropic Messages API (official SDK)
#   openai    -> the OpenAI Chat Completions API (works with any OpenAI-compatible server)
#   ollama    -> Ollama's native /api/chat (lets us control the context window size)
DEFAULT_PROVIDERS: dict[str, dict] = {
    "anthropic": {"kind": "anthropic", "api_key_env": "ANTHROPIC_API_KEY", "default_model": "claude-opus-5-5"},
    "openai": {"kind": "openai", "base_url": "https://api.openai.com/v1", "api_key_env": "OPENAI_API_KEY", "default_model": "gpt-5"},
    "openrouter": {"kind": "openai", "base_url": "https://openrouter.ai/api/v1", "api_key_env": "OPENROUTER_API_KEY"},
    "gemini": {"kind": "openai", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/", "api_key_env": "GEMINI_API_KEY"},
    "groq": {"kind": "openai", "base_url": "https://api.groq.com/openai/v1", "api_key_env": "GROQ_API_KEY"},
    "lmstudio": {"kind": "openai", "base_url": "http://localhost:1234/v1"},
    "ollama": {"kind": "ollama", "base_url": "http://localhost:11434"},
}

# Shown in the /model picker (installed Ollama models are added automatically).
MODEL_PRESETS = [
    ("anthropic:claude-opus-5-5", "Claude Opus 5.5 - most capable ($4 / $20 per MTok)"),
    ("anthropic:claude-sonnet-5-5", "Claude Sonnet 5.5 - fast everyday coding ($2 / $10)"),
    ("anthropic:claude-haiku-5-5", "Claude Haiku 5.5 - cheapest and fastest ($0.10 / $0.50)"),
    ("openai:gpt-5", "OpenAI - or type openai:<any model you have access to>"),
]

# When several local models are installed, prefer the ones that are good at tool calling.
PREFERRED_LOCAL = ("qwen3-coder", "gpt-oss", "devstral", "qwen3", "qwen3.5", "qwen2.5-coder", "llama3.1", "mistral")

MODES = ("default", "accept-edits", "plan", "yolo")
EFFORTS = ("low", "medium", "high", "xhigh", "max")


@dataclass
class Settings:
    model: str | None = None  # "provider:model", e.g. "ollama:qwen3:8b"
    mode: str = "default"
    effort: str = "high"  # Claude reasoning effort
    think: bool = True  # let local models reason before answering
    show_thinking: bool = True
    num_ctx: int = 32768  # context window we ask Ollama for
    context_limit: int | None = None  # cap on tokens we let the conversation use
    max_tool_output_chars: int = 30_000
    allow_yolo_cycle: bool = False  # include yolo in the shift+tab cycle
    providers: dict = field(default_factory=lambda: copy.deepcopy(DEFAULT_PROVIDERS))


def load_dotenv(*paths: Path) -> None:
    """A tiny .env loader: KEY=value lines. Never overrides variables already set."""
    for path in paths:
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.removeprefix("export ").strip()
            value = value.strip().strip('"').strip("'")
            if key:
                os.environ.setdefault(key, value)


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _env_int(name: str) -> int | None:
    value = os.environ.get(name, "").strip()
    return int(value) if value.isdigit() else None


def load_settings(cli: dict | None = None) -> Settings:
    cli = cli or {}
    s = Settings()

    for key, value in _read_json(HOME / "config.json").items():
        if key == "providers" and isinstance(value, dict):
            for name, cfg in value.items():
                s.providers.setdefault(name, {}).update(cfg)
        elif hasattr(s, key):
            setattr(s, key, value)

    env = {
        "model": os.environ.get("NANOCODE_MODEL"),
        "mode": os.environ.get("NANOCODE_MODE"),
        "effort": os.environ.get("NANOCODE_EFFORT"),
        "num_ctx": _env_int("NANOCODE_NUM_CTX"),
        "context_limit": _env_int("NANOCODE_CONTEXT_LIMIT"),
    }
    if os.environ.get("NANOCODE_THINK", "").lower() in ("0", "false", "off", "no"):
        s.think = False
    for source in (env, cli):
        for key, value in source.items():
            if value is not None and hasattr(s, key):
                setattr(s, key, value)

    if os.environ.get("OPENAI_BASE_URL"):
        s.providers["openai"]["base_url"] = os.environ["OPENAI_BASE_URL"]
    if os.environ.get("OLLAMA_HOST"):
        host = os.environ["OLLAMA_HOST"]
        s.providers["ollama"]["base_url"] = host if host.startswith("http") else f"http://{host}"
    if s.mode not in MODES:
        s.mode = "default"
    return s


def parse_model_spec(spec: str, providers: dict) -> tuple[str, str]:
    """'ollama:qwen3:8b' -> ('ollama', 'qwen3:8b'). Without a prefix we guess from the name."""
    spec = spec.strip()
    if spec in providers:
        provider, model = spec, ""
    else:
        head, sep, tail = spec.partition(":")
        if sep and head in providers:
            provider, model = head, tail
        elif spec.startswith("claude"):
            provider, model = "anthropic", spec
        elif spec.startswith(("gpt-", "o1", "o3", "o4")):
            provider, model = "openai", spec
        else:
            provider, model = "ollama", spec
    model = model or providers[provider].get("default_model") or ""
    if not model:
        raise ValueError(f"No model name given for '{provider}'. Use {provider}:<model-name>.")
    return provider, model


def ollama_models(base_url: str, timeout: float = 1.5) -> list[dict]:
    """Installed Ollama models, or [] if Ollama isn't running."""
    try:
        response = httpx.get(f"{base_url.rstrip('/')}/api/tags", timeout=timeout)
        response.raise_for_status()
        models = response.json().get("models", [])
    except (httpx.HTTPError, ValueError):
        return []
    out = []
    for m in models:
        caps = m.get("capabilities")  # newer Ollama versions report this
        out.append({
            "name": m["name"],
            "tools": caps is None or "tools" in caps,
            "thinking": bool(caps and "thinking" in caps),
            "size": m.get("details", {}).get("parameter_size", ""),
        })
    return out


def detect_default_model(s: Settings) -> str | None:
    """Pick a sensible model: Claude if you have a key, then OpenAI, then the best local model."""
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return f"anthropic:{s.providers['anthropic']['default_model']}"
    if os.environ.get("OPENAI_API_KEY"):
        return f"openai:{s.providers['openai']['default_model']}"
    local = pick_local_model([m["name"] for m in ollama_models(s.providers["ollama"]["base_url"]) if m["tools"]])
    return f"ollama:{local}" if local else None


def pick_local_model(names: list[str]) -> str | None:
    """Prefer the families in PREFERRED_LOCAL. Compare whole family names, so 'qwen3' != 'qwen3.5'."""
    for family in PREFERRED_LOCAL:
        for name in names:
            if name.split(":")[0] == family:
                return name
    return names[0] if names else None
