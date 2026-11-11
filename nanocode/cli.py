"""Command-line entry point: `nanocode`, `nanocode "fix the tests"`, `nanocode -p "..."`."""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from . import __version__
from .agent import Agent
from .commands import COMMANDS, attach_session, dispatch, expand_mentions, save_session
from .sessions import SessionStore
from .config import EFFORTS, HOME, MODES, detect_default_model, load_dotenv, load_settings
from .providers import ProviderError
from .ui import THEME, TerminalUI

SETUP_HELP = """\
nanocode needs a model. Pick one:

  [bold]Claude[/]     export ANTHROPIC_API_KEY=sk-ant-...      (or put it in a .env file)
  [bold]OpenAI[/]     export OPENAI_API_KEY=sk-...
  [bold]Local[/]      install Ollama (https://ollama.com), then:  ollama pull qwen3:8b

Then run [bold]nanocode[/] again, or choose explicitly:  nanocode --model ollama:qwen3:8b"""


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="nanocode", description="A Claude Code-style coding agent, built from scratch.")
    p.add_argument("prompt", nargs="*", help="an initial prompt (or the task, with -p)")
    p.add_argument("-p", "--print", action="store_true", help="non-interactive: run the task, print the answer, exit")
    p.add_argument("-m", "--model", help="provider:model, e.g. anthropic:claude-sonnet-5-5 or ollama:qwen3:8b")
    p.add_argument("--mode", choices=MODES, help="starting permission mode")
    p.add_argument("--yolo", action="store_true", help="auto-approve every action (use with care)")
    p.add_argument("--effort", choices=EFFORTS, help="Claude reasoning effort")
    p.add_argument("--num-ctx", type=int, dest="num_ctx", help="context window to request from Ollama (default 32768)")
    p.add_argument("--context-limit", type=int, dest="context_limit", help="cap on context tokens before compaction")
    p.add_argument("--no-think", action="store_true", help="turn off reasoning for local models (faster)")
    p.add_argument("-C", "--cwd", help="project directory to work in")
    p.add_argument("-c", "--continue", action="store_true", dest="resume", help="continue the most recent session here")
    p.add_argument("--no-mcp", action="store_true", help="don't start MCP servers")
    p.add_argument("--version", action="version", version=f"nanocode {__version__}")
    return p.parse_args(argv)


def start_mcp(agent: Agent, ui: TerminalUI) -> None:
    import atexit

    from .mcp import McpManager

    manager = McpManager(agent.root, HOME)
    tools = manager.start_all()
    agent.add_tools(tools)
    atexit.register(manager.close_all)
    for server in manager.summary():
        if server["status"] == "connected":
            ui.info(f"MCP: {server['name']} connected ({len(server['tools'])} tools)")
        elif server["status"] == "failed":
            ui.warn(f"MCP: {server['name']} failed to start: {server['error'][:200]}")


def repl(agent: Agent, ui: TerminalUI, initial: str | None = None) -> None:
    ui.banner(agent)
    last_ctrl_c = 0.0
    pending = initial
    while True:
        try:
            if pending:
                text, pending = pending, None
                ui.user_echo(text)
            else:
                text = ui.read_input(agent)
        except KeyboardInterrupt:
            if time.monotonic() - last_ctrl_c < 2:
                break
            last_ctrl_c = time.monotonic()
            ui.info("Press ctrl+c again to exit")
            continue
        except EOFError:
            break
        text = text.strip()
        if not text:
            continue
        try:
            if not dispatch(agent, ui, text):
                break
        except KeyboardInterrupt:
            ui.interrupted()
        save_session(agent)
    save_session(agent)
    if agent.usage_by_model:
        COMMANDS["cost"][1](agent, ui, "")
    ui.console.print(Text("\n  Bye! ✻\n", style="muted"))


def main(argv=None) -> None:
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv and argv[0] == "serve":  # the engine behind the desktop app
        from .server import main as serve

        return serve(argv[1:])
    args = parse_args(argv)
    if args.cwd:
        os.chdir(args.cwd)
    root = Path.cwd().resolve()
    load_dotenv(root / ".env", HOME / ".env")
    settings = load_settings({"model": args.model, "mode": args.mode, "effort": args.effort,
                              "num_ctx": args.num_ctx, "context_limit": args.context_limit})
    if args.no_think:
        settings.think = False
    if args.yolo:
        settings.mode = "yolo"
    settings.allow_yolo_cycle = settings.mode == "yolo"

    prompt = " ".join(args.prompt).strip()
    if args.print:
        ui = TerminalUI(console=Console(theme=THEME, stderr=True, highlight=False), interactive=False)
        if not prompt and not sys.stdin.isatty():
            prompt = sys.stdin.read().strip()
        if not prompt:
            ui.error("Nothing to do: pass a prompt, e.g. nanocode -p \"explain this project\"")
            sys.exit(2)
    else:
        ui = TerminalUI()
    ui.show_thinking = settings.show_thinking

    if not settings.model:
        with ui.spinner("Looking for a model"):
            settings.model = detect_default_model(settings)
    if not settings.model:
        ui.console.print(Panel(SETUP_HELP, border_style="accent", title="Setup", title_align="left"))
        sys.exit(1)
    try:
        agent = Agent(settings, ui, root)
    except ProviderError as e:
        ui.error(str(e))
        ui.console.print(Panel(SETUP_HELP, border_style="accent", title="Setup", title_align="left"))
        sys.exit(1)

    if not args.no_mcp:
        start_mcp(agent, ui)
    if args.print:
        agent.run_turn(expand_mentions(agent, ui, prompt))
        print(agent.last_text())
        return
    latest = SessionStore(root).latest() if args.resume else None
    attach_session(agent, latest)
    if latest:
        ui.info(f"Continuing \"{latest.get('title') or 'session'}\" ({latest.get('turn_count', 0)} turns)")
    repl(agent, ui, prompt or None)


if __name__ == "__main__":
    main()
