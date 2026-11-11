"""The protocol server (`nanocode serve`), driven in-process exactly as the desktop app drives it."""

import json
import threading
import time

import pytest
from conftest import ScriptedProvider, call, turn

from nanocode.config import Settings
from nanocode.server import Server


class Out:
    """Collects the engine's output lines and lets a test wait for a specific one."""

    def __init__(self):
        self.lines, self.cond = [], threading.Condition()

    def write(self, text):
        with self.cond:
            for line in text.splitlines():
                if line.strip():
                    self.lines.append(json.loads(line))
            self.cond.notify_all()

    def flush(self):
        pass

    def wait(self, predicate, timeout=5.0):
        deadline = time.monotonic() + timeout
        with self.cond:
            while True:
                for item in self.lines:
                    if predicate(item):
                        return item
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError(f"timed out; got {[l.get('event') or l.get('id') for l in self.lines]}")
                self.cond.wait(remaining)

    def event(self, kind, **match):
        return self.wait(lambda m: m.get("event") == kind and all(m.get(k) == v for k, v in match.items()))

    def response(self, request_id):
        return self.wait(lambda m: m.get("id") == request_id and "event" not in m)


@pytest.fixture
def make_server(tmp_path):
    def factory(turns, mode="default"):
        out = Out()
        server = Server(tmp_path.resolve(), Settings(model="fake:scripted", mode=mode), out=out,
                        provider=ScriptedProvider(turns), start_mcp=False)
        seq = iter(range(1, 1000))

        def request(method, **params):
            rid = next(seq)
            server.handle({"id": rid, "method": method, "params": params})
            return out.response(rid)

        return server, out, request

    return factory


def test_initialize_reports_everything_the_app_needs(make_server):
    server, out, request = make_server([])
    result = request("initialize")["result"]
    assert set(result) >= {"version", "cwd", "status", "session", "sessions", "commands", "mcp", "keys"}
    assert result["status"]["mode"] == "default" and result["status"]["busy"] is False
    assert any(c["name"] == "compact" for c in result["commands"])
    assert "ANTHROPIC_API_KEY" in result["keys"]


def test_a_full_turn_with_a_permission_prompt(make_server, tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    server, out, request = make_server([
        turn("Looking.", [call("read_file", path="a.py")]),
        turn("", [call("edit_file", path="a.py", old_string="x = 1", new_string="x = 2")]),
        turn("Fixed it."),
    ])
    request("initialize")
    assert request("prompt", text="fix a.py")["result"]["turn_id"] == "t1"
    out.event("user", text="fix a.py")
    out.event("tool.call", name="read_file", label="Read", arg="a.py")
    ask = out.event("permission.request", name="edit_file")
    assert ask["preview_kind"] == "diff" and "+x = 2" in ask["preview"] and ask["title"] == "Edit file"
    assert "error" in request("prompt", text="another")  # busy
    request("permission.respond", request_id=ask["request_id"], answer="yes")
    result = out.event("tool.result", name="edit_file")
    assert result["detail_kind"] == "diff" and not result["is_error"]
    end = out.event("turn.end", turn_id="t1")
    assert end["checkpoint"]["files"] == ["a.py"]
    out.wait(lambda m: m.get("event") == "status" and m["busy"] is False and m is out.lines[-1])
    assert (tmp_path / "a.py").read_text() == "x = 2\n"
    assert any(m.get("event") == "assistant" and m["text"] == "Fixed it." for m in out.lines)

    # rewind it
    restored = request("checkpoint.restore", turn_id="t1", conversation=True)["result"]
    assert restored["restored"] == ["a.py"]
    assert [e["event"] for e in restored["transcript"]] == ["info"]  # just the "Rewound..." note
    assert (tmp_path / "a.py").read_text() == "x = 1\n"


def test_sessions_are_saved_and_replayed(make_server):
    server, out, request = make_server([turn("Hello there!")])
    request("initialize")
    request("prompt", text="hi")
    out.event("turn.end", turn_id="t1")
    out.wait(lambda m: m.get("event") == "status" and m["busy"] is False and m is out.lines[-1])
    first = request("session.list")["result"]["sessions"][0]
    assert first["title"] == "hi" and first["turns"] == 1

    request("session.new")
    loaded = request("session.load", session_id=first["id"])["result"]
    kinds = [e["event"] for e in loaded["transcript"]]
    assert kinds[0] == "user" and "assistant" in kinds
    assert server.agent.messages[0]["content"] == "hi"
    assert "error" in request("session.delete", session_id=first["id"])  # it's open


def test_plan_flow_and_mode_cycle(make_server):
    server, out, request = make_server([
        turn("", [call("exit_plan_mode", plan="1. Do the thing")]),
        turn("Implementing."),
    ], mode="plan")
    request("initialize")
    request("prompt", text="plan it")
    ask = out.event("plan.request")
    assert ask["plan"] == "1. Do the thing"
    request("plan.respond", request_id=ask["request_id"], choice="accept-edits")
    out.event("turn.end", turn_id="t1")
    out.wait(lambda m: m.get("event") == "status" and m["busy"] is False and m is out.lines[-1])
    assert server.agent.permissions.mode == "accept-edits"
    assert request("mode.cycle")["result"]["status"]["mode"] == "plan"


def test_interrupt_while_waiting_for_permission(make_server):
    server, out, request = make_server([turn("", [call("bash", command="pytest -q")])])
    request("initialize")
    request("prompt", text="run it")
    out.event("permission.request")
    request("interrupt")
    out.event("interrupted")
    out.event("turn.end", turn_id="t1")
    assert server.agent.messages[-1]["content"].startswith("[Request interrupted")


def test_shell_memory_and_slash_commands(make_server, tmp_path):
    server, out, request = make_server([])
    request("initialize")
    request("prompt", text="!echo from-the-user")
    result = out.event("tool.result", name="bash")
    assert "from-the-user" in result["detail"]
    out.event("turn.end", turn_id="t1")
    out.wait(lambda m: m.get("event") == "status" and m["busy"] is False and m is out.lines[-1])
    request("prompt", text="# use tabs")
    out.event("turn.end", turn_id="t2")
    out.wait(lambda m: m.get("event") == "status" and m["busy"] is False and m is out.lines[-1])
    assert "- use tabs" in (tmp_path / "AGENTS.md").read_text()
    request("prompt", text="/context")
    out.wait(lambda m: m.get("event") == "info" and m["message"].startswith("Context:"))
    out.event("turn.end", turn_id="t3")


def test_files_search_and_models_list(make_server, tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "cart.py").write_text("")
    server, out, request = make_server([])
    assert request("files.search", query="cart")["result"]["files"] == ["src/cart.py"]
    groups = {o["group"] for o in request("models.list")["result"]["options"]}
    assert "Anthropic" in groups
    assert "error" in request("no.such.method")


def test_switching_sessions_does_not_reorder_them(make_server):
    server, out, request = make_server([turn("one"), turn("two")])
    request("initialize")
    for i, text in enumerate(["first", "second"], 1):
        if i == 2:
            request("session.new")
        request("prompt", text=text)
        out.event("turn.end", turn_id="t1") if i == 1 else None
        out.wait(lambda m: m.get("event") == "session" and m["session"]["title"] == text and m is not None)
        out.wait(lambda m: m.get("event") == "status" and m["busy"] is False and m is out.lines[-1])
    before = [s["id"] for s in request("session.list")["result"]["sessions"]]
    request("session.load", session_id=before[1])
    request("session.load", session_id=before[0])
    assert [s["id"] for s in request("session.list")["result"]["sessions"]] == before


def test_session_title_arrives_with_the_first_prompt(make_server):
    server, out, request = make_server([turn("hello")])
    request("initialize")
    request("prompt", text="explain this project")
    user_at = next(i for i, m in enumerate(out.lines) if m.get("event") == "user")
    out.wait(lambda m: m.get("event") == "session")
    session_at = next(i for i, m in enumerate(out.lines) if m.get("event") == "session")
    assert session_at == user_at + 1 and out.lines[session_at]["session"]["title"] == "explain this project"


def test_model_labels_are_human_readable(make_server):
    server, out, request = make_server([])
    labels = {o["spec"]: o["label"] for o in request("models.list")["result"]["options"]}
    assert labels["anthropic:claude-opus-5-5"] == "Claude Opus 5.5"
    assert labels["openai:gpt-5"] == "gpt-5"
