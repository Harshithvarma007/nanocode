"""Stage 2 - One tool + a loop = an agent.

We give the model a single tool, `bash`, and keep calling it until it stops asking to use
tools. That inner `while True` is the entire idea behind Claude Code, Cursor's agent, Codex...

    python stages/02_agent_loop.py
    > what python files are in this folder, and what do they do?
"""

import json
import os
import subprocess

from openai import OpenAI

client = OpenAI(base_url=os.getenv("BASE_URL", "http://localhost:11434/v1"), api_key=os.getenv("API_KEY", "ollama"))
MODEL = os.getenv("MODEL", "qwen3:8b")

TOOLS = [{
    "type": "function",
    "function": {
        "name": "bash",
        "description": "Run a shell command in the current directory and return its output.",
        "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]},
    },
}]


def bash(command: str) -> str:
    result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=120)
    return (result.stdout + result.stderr)[-10_000:] or "(no output)"


messages = [{"role": "system", "content": "You are a coding agent working in the user's current directory. "
                                          "Use the bash tool to inspect and change files. Keep going until the task is done."}]

while True:
    messages.append({"role": "user", "content": input("\n> ")})

    while True:  # <- the agent loop
        reply = client.chat.completions.create(model=MODEL, messages=messages, tools=TOOLS).choices[0].message
        messages.append(reply.model_dump(exclude_none=True))

        if not reply.tool_calls:  # no tool calls means the model is done
            print(reply.content)
            break

        for call in reply.tool_calls:
            command = json.loads(call.function.arguments)["command"]
            if input(f"  run `{command}`? [y/N] ").strip().lower() == "y":  # never run model commands blindly
                output = bash(command)
            else:
                output = "The user declined to run this command."
            print("  " + output[:300].replace("\n", "\n  "))
            messages.append({"role": "tool", "tool_call_id": call.id, "content": output})
