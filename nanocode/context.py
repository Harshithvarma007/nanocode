"""Context engineering: keeping the model's working memory useful.

The context window is everything the model can "see": system prompt + tool definitions +
the whole conversation. Fill it with junk and the agent gets slow, expensive and dumb.
nanocode manages it in layers:

  1. Prevent  - every tool result is capped at the source (see tools.truncate_middle).
  2. Trim     - at ~60% full, old tool results are replaced by a short placeholder.
  3. Compact  - at ~85% full, the model writes a handoff summary and we restart from it.

Plus a frozen system prompt (so providers can cache it) and project memory (AGENTS.md).
"""

from __future__ import annotations

import json

from .prompts import COMPACT_PROMPT

MICROCOMPACT_AT = 0.60
COMPACT_AT = 0.85
KEEP_RECENT_TOOL_RESULTS = 4
CLEARED = "[Old tool result cleared to save context. Run the tool again if you need this information.]"


def estimate_tokens(text: str) -> int:
    """~4 characters per token: rough, but good enough for budgeting."""
    return (len(text) + 3) // 4


def estimate_message_tokens(message: dict) -> int:
    n = estimate_tokens(message.get("content") or "") + 4
    for call in message.get("tool_calls") or []:
        n += estimate_tokens(json.dumps(call.get("args", {}))) + 10
    return n


def estimate_messages(messages: list[dict]) -> int:
    return sum(estimate_message_tokens(m) for m in messages)


def microcompact(messages: list[dict], keep: int = KEEP_RECENT_TOOL_RESULTS) -> tuple[list[dict], int, int]:
    """Clear the contents of all but the `keep` most recent tool results.

    The model still sees THAT it called read_file and what came next - just not 300 lines of a
    file it read 20 steps ago. Returns (new_messages, results_cleared, tokens_freed).
    """
    tool_positions = [i for i, m in enumerate(messages) if m["role"] == "tool"]
    old = set(tool_positions[:-keep] if keep else tool_positions)
    out, cleared, freed = [], 0, 0
    for i, m in enumerate(messages):
        content = m.get("content") or ""
        if i in old and content != CLEARED and len(content) > 300:
            freed += estimate_tokens(content) - estimate_tokens(CLEARED)
            cleared += 1
            m = {**m, "content": CLEARED}
        out.append(m)
    return out, cleared, freed


def compact_request(messages: list[dict], instructions: str = "") -> list[dict]:
    """Ask for the summary by APPENDING one message to the existing conversation.

    Same system prompt, same tools, same history: the provider's prompt cache still matches, so
    on Claude the summary costs a fraction of re-sending everything as a fresh transcript.
    """
    prompt = COMPACT_PROMPT + (f"\n\nAlso follow these instructions from the user: {instructions}" if instructions else "")
    return messages + [{"role": "user", "content": prompt}]


def summary_message(summary: str, todos: list[dict], continue_task: bool) -> dict:
    parts = ["This session continues an earlier conversation that was compacted to save context. "
             "Here is the summary of everything so far:", summary.strip()]
    if todos:
        parts.append("Current todo list:\n" + todos_text(todos))
    if continue_task:
        parts.append("Continue the task from where you left off. Don't ask the user to repeat anything - "
                     "pick up at the next step.")
    return {"role": "user", "content": "\n\n".join(parts)}


def todos_text(todos: list[dict]) -> str:
    marks = {"completed": "[x]", "in_progress": "[~]", "pending": "[ ]"}
    return "\n".join(f"- {marks.get(t['status'], '[ ]')} {t['content']}" for t in todos)
