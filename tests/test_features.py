"""Checkpoints, sessions, subagents, MCP, web fetch and cancellation."""

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from conftest import FakeUI, ScriptedProvider, call, turn

from nanocode.agent import Agent
from nanocode.config import Settings
from nanocode.mcp import McpManager, tool_name
from nanocode.permissions import Permissions
from nanocode.sessions import SessionStore, restore, snapshot, transcript_from_messages
from nanocode.tools import TOOLS, ToolContext, execute
from nanocode.webfetch import html_to_text

FIXTURE = Path(__file__).parent / "fixtures" / "echo_mcp_server.py"


def make_agent(tmp_path, turns, mode="default", answers=None):
    provider = ScriptedProvider(turns)
    agent = Agent(Settings(model="fake:scripted", mode=mode), FakeUI(answers), tmp_path.resolve(), provider=provider)
    return agent, provider


# ---------------------------------------------------------------- checkpoints

def test_checkpoint_restores_edited_and_created_files(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    agent, _ = make_agent(tmp_path, [
        turn("", [call("read_file", path="a.py")]),
        turn("", [call("edit_file", path="a.py", old_string="x = 1", new_string="x = 2"),
                  call("write_file", path="new/b.py", content="print(1)\n")]),
        turn("done"),
        turn("", [call("read_file", path="a.py")]),
        turn("", [call("edit_file", path="a.py", old_string="x = 2", new_string="x = 3")]),
        turn("done again"),
    ], mode="accept-edits")
    first = agent.run_turn("change things")
    agent.run_turn("change again")
    assert [c["turn_id"] for c in agent.checkpoints] == ["t1", "t2"]
    assert (tmp_path / "a.py").read_text() == "x = 3\n"

    restored = agent.restore_checkpoint(first, conversation=True)
    assert restored == ["a.py", "new/b.py"]
    assert (tmp_path / "a.py").read_text() == "x = 1\n" and not (tmp_path / "new/b.py").exists()
    assert agent.messages == [] and agent.checkpoints == []


def test_checkpoint_code_only_keeps_conversation(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    agent, _ = make_agent(tmp_path, [
        turn("", [call("read_file", path="a.py")]),
        turn("", [call("edit_file", path="a.py", old_string="x = 1", new_string="x = 2")]),
        turn("done"),
    ], mode="accept-edits")
    agent.run_turn("edit")
    count = len(agent.messages)
    agent.restore_checkpoint("t1")
    assert (tmp_path / "a.py").read_text() == "x = 1\n" and len(agent.messages) == count


# ---------------------------------------------------------------- sessions

def test_session_roundtrip_keeps_raw_blocks(tmp_path):
    agent, _ = make_agent(tmp_path, [turn("hi")])
    agent.run_turn("hello")
    agent.messages.append({"role": "assistant", "content": "x", "tool_calls": [],
                           "raw": [{"type": "thinking", "thinking": "t", "signature": "s"}], "raw_kind": "anthropic"})
    store = SessionStore(tmp_path.resolve())
    store.save(snapshot(agent, "20260101-000000-abc123", "Greeting", 1.0, [{"event": "user", "text": "hello"}]))
    data = store.load("20260101-000000-abc123")
    assert store.list()[0]["title"] == "Greeting"

    other, _ = make_agent(tmp_path, [])
    restore(other, data)
    assert other.messages == json.loads(json.dumps(agent.messages))
    assert other.messages[-1]["raw"][0]["signature"] == "s"
    with pytest.raises(ValueError):
        store.path("../escape")


def test_transcript_from_messages():
    messages = [{"role": "user", "content": "fix it"},
                {"role": "assistant", "content": "ok", "tool_calls": [{"id": "c1", "name": "read_file", "args": {"path": "a"}}]},
                {"role": "tool", "tool_call_id": "c1", "name": "read_file", "content": "1\tx"},
                {"role": "user", "content": "<system-reminder>plan</system-reminder>"}]
    events = [e["event"] for e in transcript_from_messages(messages)]
    assert events == ["user", "assistant", "tool.call", "tool.result"]


# ---------------------------------------------------------------- subagents

def test_subagent_researches_and_reports(tmp_path):
    (tmp_path / "auth.py").write_text("def login(): pass\n")
    agent, provider = make_agent(tmp_path, [
        turn("", [call("task", description="Find login", prompt="Where is login defined?")]),
        turn("", [call("read_file", path="auth.py"), call("write_file", path="x.py", content="no")]),  # the subagent
        turn("REPORT: login is in auth.py:1"),  # the subagent's report
        turn("It's in auth.py."),  # back in the parent
    ])
    agent.run_turn("where is login?")
    task_result = next(m for m in agent.messages if m["role"] == "tool" and m["name"] == "task")
    assert task_result["content"] == "REPORT: login is in auth.py:1"
    child_request = provider.requests[1]
    assert {t["name"] for t in child_request["tools"]} == {"read_file", "glob", "grep", "bash", "web_fetch"}
    assert child_request["messages"][0]["content"] == "Where is login defined?"
    assert "research subagent" in child_request["system"]
    assert ("subcall", "read_file") in agent.ui.events
    assert not (tmp_path / "x.py").exists()


def test_explore_mode_is_read_only(ctx):
    p = Permissions("explore")
    assert p.check(TOOLS["edit_file"], {"path": "a"}, ctx)[0] == "deny"
    assert p.check(TOOLS["bash"], {"command": "ls"}, ctx)[0] == "allow"
    assert p.check(TOOLS["bash"], {"command": "rm -rf x"}, ctx)[0] == "deny"


# ---------------------------------------------------------------- MCP

@pytest.fixture
def mcp_project(tmp_path):
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {
        "echo": {"command": sys.executable, "args": [str(FIXTURE)]},
        "broken": {"command": sys.executable, "args": ["-c", "import sys; sys.exit(3)"]},
        "off": {"command": "nothing", "disabled": True},
    }}))
    manager = McpManager(tmp_path.resolve(), tmp_path / "home")
    tools = manager.start_all()
    yield tmp_path, manager, tools
    manager.close_all()


def test_mcp_client_lists_and_calls_tools(mcp_project):
    root, manager, tools = mcp_project
    assert set(tools) == {"mcp__echo__echo", "mcp__echo__add"}
    assert "$schema" not in tools["mcp__echo__echo"].parameters
    status = {s["name"]: s["status"] for s in manager.summary()}
    assert status == {"echo": "connected", "broken": "failed", "off": "disabled"}
    ctx = ToolContext(root=root.resolve())
    result = execute(ctx, "mcp__echo__add", {"a": 2, "b": 3}, tools["mcp__echo__add"])
    assert result.output == "5" and not result.is_error


def test_mcp_tools_ask_permission_then_remember(mcp_project):
    root, manager, tools = mcp_project
    agent, _ = make_agent(root, [
        turn("", [call("mcp__echo__echo", text="hello")]),
        turn("", [call("mcp__echo__echo", text="again")]),
        turn("done"),
    ], answers=[("always", "")])
    agent.add_tools(tools)
    agent.run_turn("echo twice")
    results = [m["content"] for m in agent.messages if m["role"] == "tool"]
    assert results == ["hello", "again"]
    assert len([e for e in agent.ui.events if e[0] == "ask"]) == 1  # the second call was remembered


def test_tool_name_is_api_safe():
    assert tool_name("my server", "do.thing") == "mcp__my_server__do_thing"
    assert len(tool_name("s" * 40, "t" * 40)) == 64


# ---------------------------------------------------------------- web fetch

def test_html_to_text_keeps_structure():
    title, text = html_to_text("<html><head><title>Docs</title><script>evil()</script></head><body>"
                               "<h1>Install</h1><p>Run <code>pip install x</code>.</p>"
                               "<ul><li>One</li><li>Two</li></ul><pre>a = 1\n  b = 2</pre>"
                               "<a href='/guide'>Guide</a></body></html>", "https://example.com/docs")
    assert title == "Docs" and "evil" not in text
    assert "# Install" in text and "`pip install x`" in text and "- One" in text
    assert "a = 1\n  b = 2" in text and "Guide (https://example.com/guide)" in text


def test_web_fetch_end_to_end_and_domain_permission(tmp_path, ctx):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b"<html><title>Hi</title><body><h2>Hello world</h2></body></html>"
            self.send_response(200)
            self.send_header("content-type", "text/html; charset=utf-8")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}/page"
    try:
        result = execute(ctx, "web_fetch", {"url": url})
        assert "## Hello world" in result.output and "Title: Hi" in result.output
        assert execute(ctx, "web_fetch", {"url": "file:///etc/passwd"}).is_error
        p = Permissions("plan")
        assert p.check(TOOLS["web_fetch"], {"url": url}, ctx)[0] == "ask"
        p.remember("web_fetch", {"url": url})
        assert p.check(TOOLS["web_fetch"], {"url": url + "?q=1"}, ctx)[0] == "allow"
    finally:
        httpd.shutdown()


# ---------------------------------------------------------------- cancellation

def test_cancel_kills_a_running_command(ctx):
    threading.Timer(0.3, ctx.cancel.set).start()
    started = time.monotonic()
    with pytest.raises(KeyboardInterrupt):
        execute(ctx, "bash", {"command": "sleep 10"})
    assert time.monotonic() - started < 3


def test_dont_ask_again_ends_with_the_conversation(tmp_path):
    agent, _ = make_agent(tmp_path, [])
    agent.permissions.remember("bash", {"command": "pytest -q"})
    agent.permissions.remember("web_fetch", {"url": "https://docs.python.org/3/"})
    agent.reset()
    assert not agent.permissions.allowed_commands and not agent.permissions.allowed_domains


def test_subagent_keeps_the_parents_approved_domains(tmp_path):
    agent, _ = make_agent(tmp_path, [
        turn("", [call("task", description="Look", prompt="look around")]),
        turn("report"),
        turn("done"),
    ])
    agent.permissions.remember("web_fetch", {"url": "https://docs.python.org/3/"})
    agent.run_turn("research")
    assert agent.permissions.allowed_domains == {"docs.python.org"}
    task = next(m for m in agent.messages if m["role"] == "tool" and m["name"] == "task")
    assert task["content"] == "report"


def test_system_prompt_mentions_python3_when_there_is_no_python(tmp_path, monkeypatch):
    import nanocode.prompts as prompts

    monkeypatch.setattr(prompts.shutil, "which", lambda name: "/usr/bin/python3" if name == "python3" else None)
    assert "`python3`" in prompts.build_system_prompt(tmp_path, [])
    monkeypatch.setattr(prompts.shutil, "which", lambda name: "/usr/bin/" + name)
    assert "there is no `python`" not in prompts.build_system_prompt(tmp_path, [])
