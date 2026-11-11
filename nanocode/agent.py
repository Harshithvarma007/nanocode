"""The agent loop - the heart of nanocode.

    while True:
        response = model(system, messages, tools)
        if no tool calls: break             # the model is done
        run each tool call, append results  # and go around again

Everything else in this file is what makes that loop safe and pleasant to use: permissions,
retries, interrupts, plan mode, checkpoints, subagents and context management.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from . import context
from .config import HOME, Settings
from .permissions import Permissions
from .prompts import (PLAN_MODE_EXIT_REMINDER, PLAN_MODE_REMINDER, build_subagent_prompt, build_system_prompt,
                      empty_reply_reminder, load_memory, todo_reminder)
from .providers import AssistantTurn, ProviderError, Usage, make_provider
from .tools import TOOLS, Tool, ToolContext, ToolResult, execute, preview_change, validate_args

INTERRUPTED = "[Request interrupted by user]"
SUBAGENT_TOOLS = ("read_file", "glob", "grep", "bash", "web_fetch")
MAX_EMPTY_REPLIES = 2  # nudges before giving up on a model that keeps returning nothing
TODO_STALE_AFTER = 6  # tool calls without a todo_write before we remind the model
REPEATED_FAILURE = 3  # identical failing calls before we tell the model to change approach


class SubagentUI:
    """Routes a subagent's output into its parent's UI, tagged with the parent's Task call id."""

    def __init__(self, parent_ui, call_id: str):
        self.parent, self.call_id = parent_ui, call_id

    def __getattr__(self, name):
        return getattr(self.parent, name)

    def stream_view(self, parent=None):
        return self.parent.stream_view(parent=self.call_id)

    def tool_call(self, name, args, call_id=None, parent=None):
        self.parent.tool_call(name, args, call_id=call_id, parent=self.call_id)

    def tool_result(self, name, args, result, todos=None, call_id=None, parent=None):
        self.parent.tool_result(name, args, result, call_id=call_id, parent=self.call_id)

    def interrupted(self):
        pass  # the parent reports the interrupt once


class Agent:
    max_steps = 200

    def __init__(self, settings: Settings, ui, root: Path, provider=None, tools: dict[str, Tool] | None = None,
                 system: str | None = None, permissions: Permissions | None = None):
        self.settings, self.ui, self.root = settings, ui, root
        self.ctx = ToolContext(root=root)
        self.permissions = permissions or Permissions(settings.mode)
        # The tool list is fixed for the whole session (even in plan mode we just *refuse* writes):
        # changing it mid-conversation would invalidate the provider's prompt cache.
        self.tool_map: dict[str, Tool] = dict(TOOLS) if tools is None else dict(tools)
        self.tools = [t.schema() for t in self.tool_map.values()]
        self._system_override = system
        self.provider = provider or make_provider(settings.model, settings)
        self.settings.model = self.provider.label
        self.usage_by_model: dict[str, Usage] = {}
        self.reset()

    # ------------------------------------------------------------------ session state

    def reset(self) -> None:
        """Start a fresh conversation (used at startup and by /clear)."""
        self.memory = load_memory(self.root, HOME)
        self.system = self._system_override or build_system_prompt(self.root, self.memory)  # frozen
        self.messages: list[dict] = []
        self.checkpoints: list[dict] = []
        self.turn_count = 0
        self.last_checkpoint: dict | None = None
        self.ctx.read_files.clear()
        self.ctx.todos = []
        # "Don't ask again" means for this conversation: a new one starts from a clean slate.
        for remembered in (self.permissions.allowed_commands, self.permissions.allowed_domains,
                           self.permissions.allowed_tools):
            remembered.clear()
        self._baseline: tuple[int, int] | None = None  # (message count, prompt tokens) at last response
        self._announced_mode = "default"  # the mode the model was last told about
        self._steps_since_todos = 0
        self._empty_replies = 0
        self._failures: dict[tuple[str, str], int] = {}
        self._apply_output_cap()

    def add_tools(self, tools: dict[str, Tool]) -> None:
        """Add tools (e.g. from MCP servers). Only before the first turn, so the tool list stays frozen."""
        self.tool_map.update(tools)
        self.tools = [t.schema() for t in self.tool_map.values()]

    def set_model(self, spec: str) -> None:
        self.provider = make_provider(spec, self.settings)
        self.settings.model = self.provider.label
        self._baseline = None
        self._apply_output_cap()

    def _apply_output_cap(self) -> None:
        # No single tool result may eat more than ~15% of the context budget.
        budget_chars = int(self.context_limit() * 4 * 0.15)
        self.ctx.max_output_chars = max(2_000, min(self.settings.max_tool_output_chars, budget_chars))

    # ------------------------------------------------------------------ context accounting

    def context_limit(self) -> int:
        window = self.provider.context_window
        if self.settings.context_limit:
            return min(window, self.settings.context_limit)
        # A 1M-token window doesn't mean you should fill it: cost and quality both degrade.
        return window if self.provider.kind == "ollama" else min(window, 200_000)

    def context_used(self) -> int:
        """Exact prompt size from the last API response, plus an estimate for what's been added since."""
        if self._baseline:
            count, tokens = self._baseline
            return tokens + context.estimate_messages(self.messages[count:])
        return (context.estimate_tokens(self.system) + context.estimate_tokens(json.dumps(self.tools))
                + context.estimate_messages(self.messages))

    def _manage_context(self) -> None:
        limit, used = self.context_limit(), self.context_used()
        if used >= context.COMPACT_AT * limit and len(self.messages) > 1:
            self.compact(auto=True)
        elif used >= context.MICROCOMPACT_AT * limit:
            self.messages, cleared, freed = context.microcompact(self.messages)
            if cleared:
                self._baseline = None
                self.ui.info(f"Cleared {cleared} old tool results (~{freed:,} tokens) to free up context")

    def compact(self, instructions: str = "", auto: bool = False) -> bool:
        """Replace the conversation with a model-written summary of it."""
        if not self.messages:
            self.ui.info("Nothing to compact yet.")
            return False
        before = self.context_used()
        request = context.compact_request(self.messages, instructions)
        turn = None
        gen = self.provider.stream(self.system, request, self.tools)
        try:
            with self.ui.spinner("Compacting conversation"):
                for kind, data in gen:
                    if kind == "done":
                        turn = data
        except ProviderError as e:
            self.ui.error(f"Compaction failed: {e}")
            return False
        finally:
            gen.close()
        if not turn or not turn.text.strip():
            self.ui.error("Compaction failed: the model returned an empty summary.")
            return False
        self._record_usage(turn, baseline=False)
        self.messages = [context.summary_message(turn.text, self.ctx.todos, continue_task=auto)]
        self.checkpoints = []  # the conversation they pointed into is gone
        self._baseline = None
        self.ui.compacted(before, self.context_used(), self.context_limit(), turn.text)
        return True

    # ------------------------------------------------------------------ the loop

    def run_turn(self, user_text: str, label: str | None = None, turn_id: str | None = None) -> str:
        """Handle one user message: call the model and run tools until it's done. Returns the turn id."""
        self.turn_count += 1
        turn_id = turn_id or f"t{self.turn_count}"
        start = len(self.messages)
        self.ctx.checkpoint = {}  # every file this turn changes gets its original saved here
        self.ctx.cancel.clear()
        self._empty_replies, self._failures = 0, {}
        reminders = self._mode_reminders()
        content = user_text + ("\n\n" + "\n\n".join(reminders) if reminders else "")
        self.messages.append({"role": "user", "content": content})
        try:
            self._loop()
        finally:
            changed, self.ctx.checkpoint = self.ctx.checkpoint, None
            self.last_checkpoint = None
            if changed:
                self.last_checkpoint = {"turn_id": turn_id, "prompt": (label or user_text)[:200], "created": time.time(),
                                        "message_index": start, "files": {str(p): text for p, text in changed.items()}}
                self.checkpoints.append(self.last_checkpoint)
        return turn_id

    def restore_checkpoint(self, turn_id: str, conversation: bool = False) -> list[str]:
        """Put back every file changed in `turn_id` and all later turns. Optionally rewind the chat too."""
        index = next((i for i, c in enumerate(self.checkpoints) if c["turn_id"] == turn_id), None)
        if index is None:
            raise ValueError(f"No checkpoint for turn {turn_id}.")
        restored = set()
        for checkpoint in reversed(self.checkpoints[index:]):  # newest first, so the oldest original wins
            for path, original in checkpoint["files"].items():
                p = Path(path)
                if original is None:
                    p.unlink(missing_ok=True)  # the turn created it
                else:
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_text(original, encoding="utf-8", newline="")
                self.ctx.read_files.pop(p, None)
                restored.add(self.ctx.rel(p))
        if conversation:  # dropping trailing turns keeps the earlier history (and its cache) intact
            self.messages = self.messages[: self.checkpoints[index]["message_index"]]
            self.checkpoints = self.checkpoints[:index]
            self._baseline = None
        return sorted(restored)

    def _loop(self) -> None:
        for _ in range(self.max_steps):
            try:
                if self.ctx.cancel.is_set():
                    raise KeyboardInterrupt
                self._manage_context()
                turn = self._call_model()
            except KeyboardInterrupt:
                self.messages.append({"role": "user", "content": INTERRUPTED})
                self.ui.interrupted()
                return
            if turn is None:  # an error was already shown
                return
            if turn.stop_reason == "refusal":
                self.ui.error("The model declined this request." + (f" ({turn.note})" if turn.note else ""))
                return
            if not turn.is_empty:
                self.messages.append(turn.to_message())
            self._record_usage(turn)
            if turn.stop_reason == "pause_turn":
                continue
            if turn.is_empty and turn.stop_reason == "end_turn":
                # Nothing came back: no text, no tool call we could read. That is NOT "done" - small
                # models do this when a tool call's JSON was malformed (Ollama drops it silently) or
                # when they decide the next step while thinking and never take it. Say so and continue.
                if self._empty_replies < MAX_EMPTY_REPLIES:
                    self._empty_replies += 1
                    self.ui.info("The model sent back an empty response; asking it to continue")
                    self.messages.append({"role": "user", "content": empty_reply_reminder(turn.thinking)})
                    continue
                self.ui.warn("The model returned empty responses several times in a row, so nanocode stopped. "
                             "Send a message to continue, or try another model with /model.")
                return
            self._empty_replies = 0
            if not turn.tool_calls:
                if turn.stop_reason == "max_tokens":
                    self.ui.warn("The response hit the output token limit and was cut off.")
                return  # no tool calls: the model has finished
            if self._run_tools(turn.tool_calls, truncated=turn.stop_reason == "max_tokens"):
                return  # the user stopped the agent
            reminders = self._mode_reminders() + self._todo_reminders(turn.tool_calls)
            if reminders:  # e.g. the plan was approved mid-turn, or the todo list went stale
                self.messages.append({"role": "user", "content": "\n\n".join(reminders)})
        self.ui.warn(f"Stopped after {self.max_steps} steps. Send a message to continue.")

    def _call_model(self) -> AssistantTurn | None:
        for attempt in range(3):
            gen = self.provider.stream(self.system, self.messages, self.tools)
            try:
                with self.ui.stream_view() as view:
                    for kind, data in gen:
                        if kind == "done":
                            return data
                        view.update(kind, data)
                return None
            except ProviderError as e:
                last_try = attempt == 2
                if e.context_overflow and not last_try:
                    self.ui.warn("The conversation no longer fits in the context window - freeing up space.")
                    if attempt == 0:
                        self.messages, _, _ = context.microcompact(self.messages, keep=1)
                        self._baseline = None
                    elif not self.compact(auto=True):
                        return None
                    continue
                if e.retryable and not last_try:
                    self.ui.warn(f"{e} - retrying...")
                    time.sleep(2 * (attempt + 1))
                    continue
                self.ui.error(str(e))
                return None
            finally:
                gen.close()
        return None

    def _record_usage(self, turn: AssistantTurn, baseline: bool = True) -> None:
        self.usage_by_model.setdefault(self.provider.label, Usage()).add(turn.usage)
        if baseline:
            u = turn.usage
            self._baseline = (len(self.messages), u.input_tokens + u.output_tokens) if u.input_tokens else None

    def _mode_reminders(self) -> list[str]:
        """Tell the model when the mode changed. Appended, never edited: history stays append-only."""
        mode, previous = self.permissions.mode, self._announced_mode
        self._announced_mode = mode
        if mode == previous:
            return []
        if mode == "plan":
            return [PLAN_MODE_REMINDER]
        if previous == "plan":
            return [PLAN_MODE_EXIT_REMINDER]
        return []

    def _todo_reminders(self, calls: list[dict]) -> list[str]:
        """Small models write a todo list and then forget it exists. Nudge them, sparingly."""
        if any(c["name"] == "todo_write" for c in calls):
            self._steps_since_todos = 0
            return []
        self._steps_since_todos += len(calls)
        if self._steps_since_todos >= TODO_STALE_AFTER and any(t["status"] != "completed" for t in self.ctx.todos):
            self._steps_since_todos = 0
            return [todo_reminder(context.todos_text(self.ctx.todos))]
        return []

    def _note_failure(self, call: dict, result: ToolResult) -> ToolResult:
        """Break doom loops: the same failing call - or edit after edit failing on one file - gets a note."""
        args = call.get("args") if isinstance(call.get("args"), dict) else {}
        edit_key = ("edits", str(args.get("path", "")))
        if call["name"] == "edit_file" and not result.is_error:
            self._failures.pop(edit_key, None)  # a successful edit resets the streak for that file
        if not result.is_error or result.summary == "Rejected by user":
            return result
        key = (call["name"], json.dumps(args, sort_keys=True, default=str))
        self._failures[key] = self._failures.get(key, 0) + 1
        note = ""
        if self._failures[key] >= REPEATED_FAILURE:
            note = (f"\n\nThis exact call has now failed {self._failures[key]} times. Don't repeat it: re-read the "
                    "file, check the path, or try a different approach.")
        elif call["name"] == "edit_file":
            self._failures[edit_key] = self._failures.get(edit_key, 0) + 1
            if self._failures[edit_key] >= REPEATED_FAILURE:
                note = (f"\n\nedit_file has now failed {self._failures[edit_key]} times in a row on {args.get('path')}. "
                        "Stop patching it: read the whole file with read_file, then rewrite it with write_file.")
        if not note:
            return result
        return ToolResult(result.output + note, summary=result.summary, detail=result.detail,
                          detail_kind=result.detail_kind, is_error=True)

    # ------------------------------------------------------------------ tools

    def _run_tools(self, calls: list[dict], truncated: bool = False) -> bool:
        """Run a batch of tool calls. Every call gets exactly one result. Returns True to stop."""
        answered: set[str] = set()
        stop = False
        try:
            for call in calls:
                if self.ctx.cancel.is_set():  # the desktop app's Stop button
                    raise KeyboardInterrupt
                if stop:
                    result = ToolResult("Skipped: the user stopped this sequence of actions.", is_error=True)
                else:
                    result, stop = self._run_one(call, truncated)
                    result = self._note_failure(call, result)
                self._add_result(call, result)
                answered.add(call["id"])
        except KeyboardInterrupt:
            for call in calls:
                if call["id"] not in answered:
                    self._add_result(call, ToolResult("Interrupted by the user before this finished.", is_error=True))
            self.messages.append({"role": "user", "content": INTERRUPTED})
            self.ui.interrupted()
            return True
        return stop

    def _add_result(self, call: dict, result: ToolResult) -> None:
        self.messages.append({"role": "tool", "tool_call_id": call["id"], "name": call["name"],
                              "content": result.output, "is_error": result.is_error})

    def _fail(self, name: str, args: dict, message: str, call_id: str) -> tuple[ToolResult, bool]:
        result = ToolResult(f"Error: {message}", summary=message, is_error=True)
        self.ui.tool_call(name, args, call_id=call_id)
        self.ui.tool_result(name, args, result, call_id=call_id)
        return result, False

    def _run_one(self, call: dict, truncated: bool) -> tuple[ToolResult, bool]:
        name, call_id = call["name"], call["id"]
        raw_args = call["args"] if isinstance(call["args"], dict) else {}
        tool = self.tool_map.get(name)
        if tool is None:
            return self._fail(name, raw_args, f"Unknown tool '{name}'. Available tools: {', '.join(self.tool_map)}.",
                              call_id)
        if truncated:
            return self._fail(name, raw_args, "Your response hit the output token limit before this tool call was "
                              "complete, so it was not run. Try again in smaller pieces.", call_id)
        args, problem = validate_args(tool, call["args"])
        if problem:
            return self._fail(name, raw_args, problem, call_id)
        if name == "exit_plan_mode":
            return self._exit_plan_mode(args, call_id)
        if name == "task":
            return self._run_subagent(args, call_id)

        decision, reason = self.permissions.check(tool, args, self.ctx)
        if decision == "deny":
            return self._fail(name, args, reason, call_id)
        self.ui.tool_call(name, args, call_id=call_id)
        if decision == "ask":
            try:
                preview = preview_change(self.ctx, name, args)
            except Exception as e:  # e.g. old_string not found: no point asking the human
                result = ToolResult(f"Error: {e}", summary=str(e), is_error=True)
                self.ui.tool_result(name, args, result, call_id=call_id)
                return result, False
            answer, feedback = self.ui.ask_permission(name, args, preview, self.permissions, call_id=call_id)
            if answer == "no":
                message = "The user rejected this action."
                message += f" Their instructions: {feedback}" if feedback else " Stop and wait for their instructions."
                result = ToolResult(message, summary="Rejected by user", is_error=True)
                self.ui.tool_result(name, args, result, call_id=call_id)
                return result, not feedback
            if answer == "always":
                self.permissions.remember(name, args)

        with self.ui.tool_spinner(name, args):
            result = execute(self.ctx, name, args, tool)
        self.ui.tool_result(name, args, result, todos=self.ctx.todos, call_id=call_id)
        return result, False

    def _exit_plan_mode(self, args: dict, call_id: str) -> tuple[ToolResult, bool]:
        self.ui.tool_call("exit_plan_mode", args, call_id=call_id)
        if self.permissions.mode != "plan":
            result = ToolResult("Plan mode isn't active - go ahead and implement it.", summary="Not in plan mode")
            self.ui.tool_result("exit_plan_mode", args, result, call_id=call_id)
            return result, False
        choice, feedback = self.ui.ask_plan_approval(args["plan"])
        if choice == "no":
            message = "The user wants changes to the plan. Stay in plan mode."
            message += f" Their feedback: {feedback}" if feedback else " Wait for their instructions."
            result = ToolResult(message, summary="Plan rejected", is_error=True)
            self.ui.tool_result("exit_plan_mode", args, result, call_id=call_id)
            return result, not feedback
        self.permissions.mode = choice
        self._announced_mode = choice  # this tool result is the announcement
        result = ToolResult("The user approved your plan and plan mode is now off. Implement the plan, track "
                            "progress with todo_write, and verify your work when you're done.",
                            summary=f"Plan approved · mode: {choice}")
        self.ui.tool_result("exit_plan_mode", args, result, call_id=call_id)
        return result, False

    def _run_subagent(self, args: dict, call_id: str) -> tuple[ToolResult, bool]:
        """A subagent is just another Agent: fresh context, read-only tools, same model."""
        self.ui.tool_call("task", args, call_id=call_id)
        tools = {n: t for n, t in self.tool_map.items() if n in SUBAGENT_TOOLS}
        child = Agent(self.settings, SubagentUI(self.ui, call_id), self.root, provider=self.provider, tools=tools,
                      system=build_subagent_prompt(self.root, self.memory), permissions=Permissions("explore"))
        # Share approved domains both ways. Assigned after construction: the child's reset() clears its sets.
        child.permissions.allowed_domains = self.permissions.allowed_domains
        child.max_steps = 60
        child.usage_by_model = self.usage_by_model
        child.ctx.cancel = self.ctx.cancel
        child.ctx.max_output_chars = self.ctx.max_output_chars
        child.run_turn(args["prompt"])
        if self.ctx.cancel.is_set() or (child.messages and child.messages[-1].get("content") == INTERRUPTED):
            raise KeyboardInterrupt
        report = child.last_text() or "The subagent finished without writing a report."
        uses = sum(1 for m in child.messages if m["role"] == "tool")
        result = ToolResult(report, summary=f"Done ({uses} tool use{'' if uses == 1 else 's'})", detail=report,
                            detail_kind="text")
        self.ui.tool_result("task", args, result, call_id=call_id)
        return result, False

    # ------------------------------------------------------------------ helpers for commands

    def last_text(self) -> str:
        for m in reversed(self.messages):
            if m["role"] == "assistant" and m.get("content"):
                return m["content"]
        return ""
