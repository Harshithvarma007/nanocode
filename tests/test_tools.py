import time

import pytest

from nanocode.tools import (TOOLS, ToolContext, execute, glob_to_regex, preview_change, truncate_middle,
                            validate_args)


def run(ctx, name, **args):
    clean, problem = validate_args(TOOLS[name], args)
    assert problem is None, problem
    return execute(ctx, name, clean)


def test_read_file_numbers_lines_and_pages(ctx):
    (ctx.root / "a.txt").write_text("\n".join(f"line {i}" for i in range(1, 11)))
    result = run(ctx, "read_file", path="a.txt", offset=3, limit=2)
    assert "     3\tline 3" in result.output and "     4\tline 4" in result.output
    assert "line 5" not in result.output.split("[")[0]
    assert "offset=5" in result.output


def test_read_missing_file_suggests_close_match(ctx):
    (ctx.root / "config.py").write_text("x = 1\n")
    result = run(ctx, "read_file", path="confg.py")
    assert result.is_error and "config.py" in result.output


def test_must_read_before_edit(ctx):
    (ctx.root / "app.py").write_text("x = 1\n")
    result = run(ctx, "edit_file", path="app.py", old_string="x = 1", new_string="x = 2")
    assert result.is_error and "must read" in result.output


def test_edit_detects_external_changes(ctx):
    f = ctx.root / "app.py"
    f.write_text("x = 1\n")
    run(ctx, "read_file", path="app.py")
    time.sleep(0.01)
    f.write_text("x = 99\n")
    result = run(ctx, "edit_file", path="app.py", old_string="x = 99", new_string="x = 2")
    assert result.is_error and "changed since" in result.output


def test_edit_requires_unique_match(ctx):
    (ctx.root / "app.py").write_text("a = 1\na = 1\n")
    run(ctx, "read_file", path="app.py")
    result = run(ctx, "edit_file", path="app.py", old_string="a = 1", new_string="a = 2")
    assert result.is_error and "matches 2 places" in result.output
    result = run(ctx, "edit_file", path="app.py", old_string="a = 1", new_string="a = 2", replace_all=True)
    assert not result.is_error
    assert (ctx.root / "app.py").read_text() == "a = 2\na = 2\n"


def test_edit_whitespace_hint_and_success(ctx):
    (ctx.root / "app.py").write_text("def f():\n    return 1\n")
    run(ctx, "read_file", path="app.py")
    miss = run(ctx, "edit_file", path="app.py", old_string="return 1  ", new_string="return 2")
    assert miss.is_error and "whitespace" in miss.output
    ok = run(ctx, "edit_file", path="app.py", old_string="    return 1", new_string="    return 2")
    assert not ok.is_error and "return 2" in ok.output and ok.detail_kind == "diff"
    assert (ctx.root / "app.py").read_text() == "def f():\n    return 2\n"


def test_edit_preserves_windows_line_endings(ctx):
    f = ctx.root / "win.txt"
    f.write_bytes(b"one\r\ntwo\r\n")
    run(ctx, "read_file", path="win.txt")
    result = run(ctx, "edit_file", path="win.txt", old_string="one\ntwo", new_string="uno\ndos")
    assert not result.is_error
    assert f.read_bytes() == b"uno\r\ndos\r\n"


def test_write_creates_directories_and_preview_is_a_diff(ctx):
    args = {"path": "src/new/mod.py", "content": "print('hi')\n"}
    assert "@@ -0,0 +1 @@" in preview_change(ctx, "write_file", args)
    result = run(ctx, "write_file", **args)
    assert not result.is_error and (ctx.root / "src/new/mod.py").read_text() == "print('hi')\n"


def test_bash_output_and_exit_code(ctx):
    result = run(ctx, "bash", command="echo hello && exit 3")
    assert "hello" in result.output and "[exit code 3]" in result.output
    assert result.summary == "Exit code 3"


def test_bash_timeout(ctx):
    result = run(ctx, "bash", command="sleep 5", timeout=1)
    assert "timed out" in result.output


def test_glob_matches_at_any_depth_and_skips_ignored_dirs(ctx):
    for path in ["a.py", "src/b.py", "src/c.txt", "node_modules/x.py", ".venv/y.py"]:
        (ctx.root / path).parent.mkdir(parents=True, exist_ok=True)
        (ctx.root / path).write_text("")
    names = set(run(ctx, "glob", pattern="*.py").output.splitlines())
    assert names == {"a.py", "src/b.py"}
    assert run(ctx, "glob", pattern="src/*.txt").output == "src/c.txt"


def test_glob_to_regex():
    assert glob_to_regex("**/*.py").match("a/b/c.py")
    assert glob_to_regex("**/*.py").match("c.py")
    assert glob_to_regex("*.{js,ts}").match("x.ts")
    assert not glob_to_regex("src/*.py").match("src/a/b.py")


@pytest.mark.parametrize("mode,expected", [("files_with_matches", "a.py"), ("content", "a.py:2:"), ("count", "a.py:1")])
def test_grep_modes(ctx, mode, expected):
    (ctx.root / "a.py").write_text("x = 1\ndef target():\n    pass\n")
    (ctx.root / "b.py").write_text("nothing here\n")
    result = run(ctx, "grep", pattern=r"def \w+", output_mode=mode)
    assert expected in result.output and "b.py" not in result.output


def test_truncate_middle_keeps_head_and_tail():
    text = "\n".join(f"line {i}" for i in range(2000))
    out = truncate_middle(text, 1000)
    assert out.startswith("line 0") and out.endswith("line 1999") and "truncated" in out
    assert len(out) < 1200


def test_validate_args_coerces_small_model_mistakes():
    args, problem = validate_args(TOOLS["read_file"], {"path": "a.py", "limit": "20", "bogus": 1, "offset": None})
    assert problem is None and args == {"path": "a.py", "limit": 20}
    _, problem = validate_args(TOOLS["edit_file"], {"path": "a.py"})
    assert "old_string" in problem
    args, problem = validate_args(TOOLS["todo_write"], {"todos": '[{"content": "x", "status": "pending"}]'})
    assert problem is None and args["todos"][0]["content"] == "x"


def test_todo_write_validates_status(ctx):
    bad = run(ctx, "todo_write", todos=[{"content": "x", "status": "doing"}])
    assert bad.is_error
    ok = run(ctx, "todo_write", todos=[{"content": "x", "status": "in_progress"}])
    assert not ok.is_error and ctx.todos == [{"content": "x", "status": "in_progress"}]


def test_tools_never_crash_the_agent(ctx):
    result = execute(ctx, "read_file", {"path": str(ctx.root)})
    assert result.is_error and "directory" in result.output


def test_paths_resolve_relative_to_root(tmp_path):
    c = ToolContext(root=tmp_path.resolve())
    assert c.resolve("x/y.py") == tmp_path.resolve() / "x/y.py"
    assert c.inside_root(c.resolve("x/y.py")) and not c.inside_root(c.resolve("/etc/hosts"))


def test_glob_accepts_comma_separated_patterns(ctx):
    for name in ("index.html", "style.css", "app.js", "notes.md"):
        (ctx.root / name).write_text("")
    found = set(run(ctx, "glob", pattern="**/*.html, **/*.css, **/*.js").output.splitlines())
    assert found == {"index.html", "style.css", "app.js"}
