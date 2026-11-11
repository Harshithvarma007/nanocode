import contextlib
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["NANOCODE_HOME"] = str(Path(__file__).resolve().parent / ".nanocode-home")

from nanocode.providers.base import AssistantTurn, Provider  # noqa: E402
from nanocode.tools import ToolContext  # noqa: E402


@pytest.fixture
def ctx(tmp_path):
    return ToolContext(root=tmp_path.resolve())


class ScriptedProvider(Provider):
    """A fake model that replays pre-written turns - lets us test the agent loop offline."""

    name, kind, model, context_window = "fake", "fake", "scripted", 100_000

    def __init__(self, turns):
        self.turns = list(turns)
        self.requests = []

    def stream(self, system, messages, tools):
        self.requests.append({"system": system, "messages": [dict(m) for m in messages], "tools": tools})
        turn = self.turns.pop(0)
        if isinstance(turn, BaseException):
            raise turn
        if turn.text:
            yield "text", turn.text
        yield "done", turn


class FakeView:
    def update(self, kind, data):
        pass


class FakeUI:
    """Records what the agent shows, and answers permission prompts from a script."""

    interactive = True
    show_thinking = False

    def __init__(self, answers=None, plan_answer=("accept-edits", "")):
        self.answers = list(answers or [])
        self.plan_answer = plan_answer
        self.events = []

    @contextlib.contextmanager
    def stream_view(self, parent=None):
        yield FakeView()

    @contextlib.contextmanager
    def spinner(self, label):
        yield

    @contextlib.contextmanager
    def tool_spinner(self, name, args):
        yield

    def tool_call(self, name, args, call_id=None, parent=None):
        self.events.append(("call", name) if parent is None else ("subcall", name))

    def tool_result(self, name, args, result, todos=None, call_id=None, parent=None):
        self.events.append(("result", name, result.is_error))

    def ask_permission(self, name, args, preview, permissions, call_id=None):
        self.events.append(("ask", name, preview))
        return self.answers.pop(0) if self.answers else ("yes", "")

    def ask_plan_approval(self, plan):
        self.events.append(("plan", plan))
        return self.plan_answer

    def info(self, message):
        self.events.append(("info", message))

    warn = error = info

    def interrupted(self):
        self.events.append(("interrupted",))

    def compacted(self, before, after, limit, summary):
        self.events.append(("compacted", before, after))


def turn(text="", calls=None, stop=None, input_tokens=0):
    from nanocode.providers.base import Usage

    calls = calls or []
    return AssistantTurn(text=text, tool_calls=calls, stop_reason=stop or ("tool_use" if calls else "end_turn"),
                         usage=Usage(input_tokens=input_tokens, output_tokens=10 if input_tokens else 0))


def call(name, **args):
    import uuid

    return {"id": "call_" + uuid.uuid4().hex[:8], "name": name, "args": args}
