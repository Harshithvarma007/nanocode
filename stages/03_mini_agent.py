"""Stage 3 - A mini Claude Code in ~200 lines.

Real tools beat one bash tool: they're safer (we know what they do), cheaper (we control the
output size) and the model uses them more reliably (good names + descriptions + schemas).

    python stages/03_mini_agent.py
    > the tests in demo/shop are failing - find out why and fix them
"""

import json
import os
import re
import subprocess
from pathlib import Path

from openai import OpenAI

client = OpenAI(base_url=os.getenv("BASE_URL", "http://localhost:11434/v1"), api_key=os.getenv("API_KEY", "ollama"))
MODEL = os.getenv("MODEL", "qwen3:8b")
ROOT = Path.cwd()

SYSTEM = f"""You are a coding agent working in {ROOT}.
- Explore before you change anything: use list_files and read_file. Never guess what a file contains.
- Change files with edit_file (small, exact replacements) or write_file (new files).
- Verify your work with bash (run the tests or the script), and fix anything that fails.
- Keep going until the task is done, then summarize what you changed in a few lines."""


# ---------------------------------------------------------------- tools: plain Python functions

def read_file(path: str) -> str:
    lines = (ROOT / path).read_text().splitlines()
    return "\n".join(f"{i:>4}  {line}" for i, line in enumerate(lines, 1)) or "(empty file)"


def write_file(path: str, content: str) -> str:
    (ROOT / path).parent.mkdir(parents=True, exist_ok=True)
    (ROOT / path).write_text(content)
    return f"Wrote {len(content.splitlines())} lines to {path}"


def edit_file(path: str, old_string: str, new_string: str) -> str:
    text = (ROOT / path).read_text()
    count = text.count(old_string)
    if count != 1:  # errors are feedback: the model reads this and tries again
        return f"Error: old_string must appear exactly once in {path}, but it appears {count} times."
    (ROOT / path).write_text(text.replace(old_string, new_string))
    return f"Edited {path}"


def list_files(pattern: str = "**/*") -> str:
    skip = {".git", ".venv", "node_modules", "__pycache__"}
    files = [str(p.relative_to(ROOT)) for p in sorted(ROOT.glob(pattern))
             if p.is_file() and not skip & set(p.parts)]
    return "\n".join(files[:200]) or "No files found."


def bash(command: str) -> str:
    result = subprocess.run(command, shell=True, cwd=ROOT, capture_output=True, text=True, timeout=120)
    output = result.stdout + result.stderr
    if len(output) > 8000:  # keep the head and the tail: errors live at the end
        output = output[:5000] + "\n... [truncated] ...\n" + output[-3000:]
    return f"{output}\n[exit code {result.returncode}]"


# ---------------------------------------------------------------- tool definitions the model sees

def schema(name, description, **params):
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": {k: {"type": "string", "description": v} for k, v in params.items()},
                       "required": list(params)}}}


TOOLS = [
    schema("read_file", "Read a text file. Lines are prefixed with line numbers (not part of the file).",
           path="Path relative to the project root"),
    schema("write_file", "Create or overwrite a file with the given content.",
           path="Path relative to the project root", content="The full file content"),
    schema("edit_file", "Replace an exact, unique string in a file. Copy old_string exactly, including indentation.",
           path="Path relative to the project root", old_string="Exact text to replace", new_string="Replacement text"),
    schema("list_files", "List files matching a glob pattern, e.g. '**/*.py'.", pattern="Glob pattern"),
    schema("bash", "Run a shell command (tests, scripts, git). Not for reading or editing files.",
           command="The command to run"),
]
FUNCTIONS = {"read_file": read_file, "write_file": write_file, "edit_file": edit_file,
             "list_files": list_files, "bash": bash}
NEEDS_APPROVAL = {"write_file", "edit_file", "bash"}


# ---------------------------------------------------------------- the agent loop

def run_tool(name: str, args: dict) -> str:
    if name not in FUNCTIONS:
        return f"Error: unknown tool {name}"
    if name in NEEDS_APPROVAL:
        detail = args.get("command") or args.get("path")
        if input(f"  \033[33mallow {name}({detail})? [y/N]\033[0m ").strip().lower() != "y":
            return "The user denied this action. Ask them what to do instead."
    try:
        return FUNCTIONS[name](**args)
    except Exception as e:  # a failing tool must never crash the agent
        return f"Error: {type(e).__name__}: {e}"


def agent_turn(messages: list) -> None:
    while True:
        reply = client.chat.completions.create(model=MODEL, messages=messages, tools=TOOLS).choices[0].message
        messages.append(reply.model_dump(exclude_none=True))
        if reply.content:
            print(f"\n\033[1m●\033[0m {re.sub(r'<think>.*?</think>', '', reply.content, flags=re.S).strip()}")
        if not reply.tool_calls:
            return
        for call in reply.tool_calls:
            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                args, output = {}, "Error: your arguments were not valid JSON."
            else:
                print(f"\n\033[32m●\033[0m {call.function.name}({', '.join(f'{k}={str(v)[:60]!r}' for k, v in args.items())})")
                output = run_tool(call.function.name, args)
            print("  ⎿  " + output[:200].replace("\n", "\n     "))
            messages.append({"role": "tool", "tool_call_id": call.id, "content": output})


if __name__ == "__main__":
    print(f"mini-agent · {MODEL} · {ROOT}  (ctrl+c to quit)")
    history = [{"role": "system", "content": SYSTEM}]
    while True:
        try:
            history.append({"role": "user", "content": input("\n\033[1m❯\033[0m ")})
            agent_turn(history)
        except (KeyboardInterrupt, EOFError):
            print("\nbye!")
            break
