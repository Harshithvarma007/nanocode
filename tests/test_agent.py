from conftest import FakeUI, ScriptedProvider, call, turn

from nanocode.agent import INTERRUPTED, Agent
from nanocode.config import Settings
from nanocode.context import CLEARED, microcompact
from nanocode.prompts import PLAN_MODE_REMINDER


def make_agent(tmp_path, turns, mode="default", answers=None, context_limit=None, plan_answer=("accept-edits", "")):
    settings = Settings(model="fake:scripted", mode=mode, context_limit=context_limit)
    provider = ScriptedProvider(turns)
    ui = FakeUI(answers, plan_answer)
    return Agent(settings, ui, tmp_path.resolve(), provider=provider), provider, ui


def test_loop_runs_tools_until_the_model_stops(tmp_path):
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    agent, provider, ui = make_agent(tmp_path, [
        turn("Let me look.", [call("read_file", path="calc.py")]),
        turn("", [call("edit_file", path="calc.py", old_string="a - b", new_string="a + b")]),
        turn("Fixed it."),
    ])
    agent.run_turn("fix the bug")
    roles = [m["role"] for m in agent.messages]
    assert roles == ["user", "assistant", "tool", "assistant", "tool", "assistant"]
    assert "return a - b" in agent.messages[2]["content"]
    assert (tmp_path / "calc.py").read_text().endswith("return a + b\n")
    asks = [e for e in ui.events if e[0] == "ask"]
    assert len(asks) == 1 and asks[0][1] == "edit_file" and "+    return a + b" in asks[0][2]
    assert len(provider.requests) == 3 and provider.requests[0]["tools"] == agent.tools


def test_rejecting_without_feedback_stops_the_agent(tmp_path):
    (tmp_path / "a.txt").write_text("x\n")
    agent, provider, _ = make_agent(tmp_path, [
        turn("", [call("read_file", path="a.txt"), call("write_file", path="a.txt", content="y\n"),
                  call("bash", command="pytest")]),
        turn("should not be reached"),
    ], answers=[("no", "")])
    agent.run_turn("change it")
    results = [m for m in agent.messages if m["role"] == "tool"]
    assert [r["is_error"] for r in results] == [False, True, True]
    assert "Skipped" in results[2]["content"]
    assert len(provider.requests) == 1 and (tmp_path / "a.txt").read_text() == "x\n"


def test_rejecting_with_feedback_continues(tmp_path):
    agent, provider, _ = make_agent(tmp_path, [
        turn("", [call("write_file", path="a.txt", content="y\n")]),
        turn("ok, I'll use b.txt"),
    ], answers=[("no", "name it b.txt")])
    agent.run_turn("make a file")
    assert "name it b.txt" in agent.messages[2]["content"]
    assert len(provider.requests) == 2


def test_plan_mode_reminder_denial_and_approval(tmp_path):
    agent, provider, ui = make_agent(tmp_path, [
        turn("", [call("write_file", path="a.txt", content="y\n")]),
        turn("", [call("exit_plan_mode", plan="1. create a.txt")]),
        turn("", [call("write_file", path="a.txt", content="y\n")]),
        turn("done"),
    ], mode="plan")
    agent.run_turn("make a file")
    assert PLAN_MODE_REMINDER in agent.messages[0]["content"]
    results = [m for m in agent.messages if m["role"] == "tool"]
    assert results[0]["is_error"] and "Plan mode" in results[0]["content"]
    assert "approved" in results[1]["content"]
    assert agent.permissions.mode == "accept-edits"
    assert not results[2]["is_error"] and (tmp_path / "a.txt").exists()


def test_bad_tool_calls_become_feedback(tmp_path):
    agent, provider, _ = make_agent(tmp_path, [
        turn("", [call("teleport", to="mars"), call("read_file", wrong="x"),
                  {"id": "c1", "name": "glob", "args": {"__invalid_json__": "{oops"}}]),
        turn("sorry"),
    ])
    agent.run_turn("go")
    results = [m["content"] for m in agent.messages if m["role"] == "tool"]
    assert "Unknown tool" in results[0] and "Missing required" in results[1] and "not valid JSON" in results[2]


def test_interrupt_mid_stream_keeps_history_valid(tmp_path):
    agent, _, ui = make_agent(tmp_path, [KeyboardInterrupt()])
    agent.run_turn("hello")
    assert agent.messages[-1]["content"] == INTERRUPTED and ("interrupted",) in ui.events


def test_auto_compaction_replaces_history_with_summary(tmp_path):
    agent, provider, ui = make_agent(tmp_path, [
        turn("", [call("bash", command="echo " + "x" * 50)], input_tokens=900),
        turn("SUMMARY: user wants a file", input_tokens=950),
        turn("continuing"),
    ], mode="yolo", context_limit=1000)
    agent.run_turn("do the thing")
    assert any(e[0] == "compacted" for e in ui.events)
    assert "SUMMARY: user wants a file" in provider.requests[2]["messages"][0]["content"]
    assert provider.requests[1]["messages"][-1]["content"].startswith("Your context window is filling up")
    assert agent.messages[-1]["content"] == "continuing"


def test_microcompact_clears_only_old_results():
    messages = [{"role": "user", "content": "go"}]
    for i in range(6):
        messages.append({"role": "assistant", "content": "", "tool_calls": [{"id": str(i), "name": "read_file", "args": {}}]})
        messages.append({"role": "tool", "tool_call_id": str(i), "name": "read_file", "content": "y" * 1000})
    new, cleared, freed = microcompact(messages, keep=2)
    contents = [m["content"] for m in new if m["role"] == "tool"]
    assert cleared == 4 and freed > 900 and contents[:4] == [CLEARED] * 4 and contents[4:] == ["y" * 1000] * 2
    assert messages[2]["content"] == "y" * 1000  # the original list is untouched


def test_system_prompt_and_tools_are_frozen_across_turns(tmp_path):
    agent, provider, _ = make_agent(tmp_path, [turn("", [call("glob", pattern="*")]), turn("a"), turn("b")])
    agent.run_turn("one")
    agent.permissions.mode = "plan"
    agent.run_turn("two")
    systems = {r["system"] for r in provider.requests}
    assert len(systems) == 1
    assert PLAN_MODE_REMINDER in provider.requests[2]["messages"][-1]["content"]


def test_an_empty_reply_is_not_the_end_of_the_turn(tmp_path):
    agent, provider, ui = make_agent(tmp_path, [
        turn(""),  # nothing at all: e.g. a tool call whose JSON Ollama couldn't parse
        turn("", [call("glob", pattern="*")]),
        turn("Done."),
    ])
    agent.run_turn("do it")
    assert len(provider.requests) == 3
    nudge = provider.requests[1]["messages"][-1]["content"]
    assert nudge.startswith("<system-reminder>Your last response came back empty")
    assert agent.messages[-1]["content"] == "Done."
    assert any(e[0] == "info" and "empty" in e[1] for e in ui.events)


def test_repeated_empty_replies_stop_with_a_warning(tmp_path):
    agent, provider, ui = make_agent(tmp_path, [turn(""), turn(""), turn(""), turn("never")])
    agent.run_turn("do it")
    assert len(provider.requests) == 3
    assert any(e[0] == "info" and "several times" in e[1] for e in ui.events)


def test_stale_todo_list_gets_a_reminder(tmp_path):
    todos = [{"content": "a", "status": "in_progress"}, {"content": "b", "status": "pending"}]
    turns = [turn("", [call("todo_write", todos=todos)])]
    turns += [turn("", [call("glob", pattern="*")]) for _ in range(6)]
    turns += [turn("done")]
    agent, provider, _ = make_agent(tmp_path, turns)
    agent.run_turn("work")
    reminders = [m for m in agent.messages if m["role"] == "user" and "todo list hasn't been updated" in m["content"]]
    assert len(reminders) == 1 and "[~] a" in reminders[0]["content"]


def test_the_same_failing_call_three_times_gets_a_pointed_note(tmp_path):
    bad = {"path": "missing.py"}
    agent, provider, _ = make_agent(tmp_path, [turn("", [call("read_file", **bad)]) for _ in range(3)] + [turn("ok")])
    agent.run_turn("read it")
    results = [m["content"] for m in agent.messages if m["role"] == "tool"]
    assert "failed 3 times" not in results[1] and "failed 3 times" in results[2]


def test_three_failed_edits_on_one_file_suggest_a_rewrite(tmp_path):
    (tmp_path / "a.js").write_text("let x = 1;\n")
    edits = [call("edit_file", path="a.js", old_string=f"nope{i}", new_string="y") for i in range(3)]
    agent, provider, _ = make_agent(tmp_path, [turn("", [call("read_file", path="a.js")])]
                                    + [turn("", [e]) for e in edits] + [turn("ok")], mode="accept-edits")
    agent.run_turn("edit it")
    results = [m["content"] for m in agent.messages if m["role"] == "tool"]
    assert "rewrite it with write_file" not in results[2] and "rewrite it with write_file" in results[3]
