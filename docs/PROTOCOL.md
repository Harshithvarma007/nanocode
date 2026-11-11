# nanocode engine protocol (v1)

The desktop app never runs agent logic itself. It starts the Python engine as a child process and
talks to it over **newline-delimited JSON on stdin/stdout**. stderr is log output only. The split
(an agent server, thin clients on top) follows OpenCode's architecture (`anomalyco/opencode`,
MIT): its desktop app, TUI and web clients all drive one server.

```
nanocode serve --cwd <project folder> [--model provider:model]
```

One engine process per project window. Every line in either direction is one JSON object.

## Requests and responses

Client → engine:

```json
{"id": 7, "method": "prompt", "params": {"text": "fix the failing tests"}}
```

Engine → client, exactly one response per request id:

```json
{"id": 7, "result": {"turn_id": "t3"}}
{"id": 7, "error": {"message": "A turn is already running."}}
```

Long-running work (a turn, a compaction) answers immediately, then reports progress as events.

## Events

Engine → client, no id:

```json
{"event": "text", "delta": "Let me look at the tests."}
```

Events that describe the conversation (marked **[log]** below) are also recorded in the session,
and `session.load` returns them as `transcript`, so the client renders history with the same code
it uses for live events.

## Shared shapes

```ts
type Mode = "default" | "accept-edits" | "plan" | "yolo";

type SessionInfo = {
  id: string; title: string; created: number; updated: number;  // unix seconds
  model: string; turns: number;
};

type Todo = { content: string; status: "pending" | "in_progress" | "completed" };

type Checkpoint = {
  turn_id: string; prompt: string; created: number;
  files: string[];  // project-relative paths the turn changed (with write_file / edit_file)
};

type Status = {
  mode: Mode; model: string; busy: boolean;
  context_used: number; context_limit: number;
};
```

## Methods

| Method | Params | Result | Notes |
|---|---|---|---|
| `initialize` | `{}` | `{version, cwd, status: Status, effort, think, session: SessionInfo, sessions: SessionInfo[], commands: {name, description}[], mcp: McpServer[], keys: {[env: string]: boolean}}` | Call first. Starts a new empty session. `keys` says which API-key env vars are set (never the values). |
| `prompt` | `{text}` | `{turn_id}` | Starts a turn. `text` may start with `/` (slash command), `!` (run a shell command yourself) or `#` (save a memory note), like the TUI. `@path` mentions attach files. Errors if busy. |
| `interrupt` | `{}` | `{}` | Stops the running turn as soon as possible (kills a running shell command too). |
| `permission.respond` | `{request_id, answer: "yes" \| "always" \| "no", feedback?}` | `{}` | Answers a `permission.request`. |
| `plan.respond` | `{request_id, choice: "accept-edits" \| "default" \| "no", feedback?}` | `{}` | Answers a `plan.request`. |
| `mode.set` | `{mode}` | `{status}` | |
| `mode.cycle` | `{}` | `{status}` | default → accept-edits → plan (→ yolo only if started with `--yolo`). Shift+Tab. |
| `models.list` | `{}` | `{current, options: ModelOption[]}` | `ModelOption = {spec, label, detail, group, available, needs?}`; `group` is "Anthropic", "OpenAI-compatible" or "Local (Ollama)"; `needs` names the missing key env var. |
| `model.set` | `{spec}` | `{status}` | Errors with a readable message if the model can't be reached. |
| `effort.set` | `{effort}` | `{}` | low · medium · high · xhigh · max (Claude). |
| `think.set` | `{on}` | `{}` | Reasoning for local models + whether thinking is shown. |
| `compact` | `{instructions?}` | `{}` | Runs like a turn: `busy` status, then a `compacted` event. |
| `context.get` | `{}` | `{used, limit, window, system, tools, messages, microcompact_at, compact_at}` | Token counts; thresholds are fractions (0.6, 0.85). |
| `cost.get` | `{}` | `{rows: {model, input, cached, output, cost}[]}` | `cost` is a display string ("$0.0123", "$0.00 (local)", "not tracked"). |
| `todos.get` | `{}` | `{todos: Todo[]}` | |
| `session.list` | `{}` | `{sessions: SessionInfo[]}` | Newest first, for this project. |
| `session.new` | `{}` | `{session}` | Ends nothing: the old session stays saved. |
| `session.load` | `{session_id}` | `{session, transcript: Event[], todos: Todo[], status}` | Errors if busy. |
| `session.rename` | `{session_id, title}` | `{session}` | |
| `session.delete` | `{session_id}` | `{}` | Can't delete the open session. |
| `checkpoint.list` | `{}` | `{checkpoints: Checkpoint[]}` | Current session, oldest first. |
| `checkpoint.restore` | `{turn_id, conversation: boolean}` | `{restored: string[], transcript?: Event[], status}` | Puts every file changed in that turn **and all later turns** back to how it was before that turn. With `conversation: true` the conversation is also rewound to just before that prompt, and the new `transcript` is returned. Shell-command side effects are not undone. |
| `files.search` | `{query, limit?}` | `{files: string[]}` | For `@` autocomplete. Project-relative paths. |
| `mcp.list` | `{}` | `{servers: McpServer[]}` | `McpServer = {name, status: "connected" \| "failed" \| "disabled", tools: string[], error?}` |
| `shutdown` | `{}` | `{}` | Saves and exits. |

## Events

| Event | Fields | |
|---|---|---|
| `ready` | `{version}` | First line the engine prints. Then the client sends `initialize`. |
| `status` | `Status` | Whenever mode, model, busy or context usage changes. |
| `user` | `{turn_id, text}` | **[log]** The user's message (after `@` expansion is *not* shown: `text` is what they typed). |
| `stream.start` | `{parent?}` | A model call began: show a spinner. |
| `thinking` | `{delta, parent?}` | Streaming reasoning. |
| `text` | `{delta, parent?}` | Streaming answer text. |
| `tool.preparing` | `{name, parent?}` | The model started writing a call to this tool. |
| `assistant` | `{text, thinking, thinking_seconds, parent?}` | **[log]** The finished text of one model call (may be empty when it only called tools). Replaces the streamed deltas. |
| `tool.call` | `{call_id, name, label, arg, args, parent?}` | **[log]** `label` is the display name ("Read", "Update", "Bash", "Web", "Task", or "mcp: server/tool"); `arg` is the one-line summary ("src/app.py", the command…). |
| `permission.request` | `{request_id, call_id, name, title, question, preview, preview_kind, always_label}` | Show inline approval. `preview` is a unified diff (`preview_kind: "diff"`), a command (`"command"`), a URL (`"url"`) or JSON args (`"json"`). |
| `plan.request` | `{request_id, plan}` | `plan` is Markdown. |
| `tool.result` | `{call_id, name, summary, detail, detail_kind, is_error, parent?}` | **[log]** `detail_kind`: `"diff"` (unified diff), `"text"` (command/fetch output), `"todos"`, or `""`. |
| `todos` | `{todos}` | **[log]** The current list after a `todo_write`. |
| `info` / `warn` / `error` | `{message}` | **[log]** |
| `compacted` | `{before, after, limit, summary}` | **[log]** |
| `interrupted` | `{}` | **[log]** |
| `turn.end` | `{turn_id, checkpoint: Checkpoint \| null}` | The turn is over (finished, stopped or failed). `checkpoint` is set if the turn changed files. |
| `session` | `{session: SessionInfo}` | The open session's info changed (title set from the first prompt, turn count…). |

`parent` is the `call_id` of a `task` tool call: events from a **subagent** carry it, so the client
can nest them inside that Task card.

## A turn, end to end

```
→ {"id":3,"method":"prompt","params":{"text":"fix the tests"}}
← {"id":3,"result":{"turn_id":"t1"}}
← {"event":"user","turn_id":"t1","text":"fix the tests"}
← {"event":"status","busy":true,...}
← {"event":"stream.start"}
← {"event":"text","delta":"Let me look."}
← {"event":"assistant","text":"Let me look.","thinking":"","thinking_seconds":0}
← {"event":"tool.call","call_id":"c1","name":"read_file","label":"Read","arg":"test_cart.py","args":{...}}
← {"event":"tool.result","call_id":"c1","name":"read_file","summary":"Read 41 lines","detail":"","detail_kind":"","is_error":false}
← {"event":"stream.start"}
← {"event":"assistant","text":"","thinking":"",...}
← {"event":"tool.call","call_id":"c2","name":"edit_file","label":"Update","arg":"cart.py",...}
← {"event":"permission.request","request_id":"p1","call_id":"c2","title":"Edit file","question":"Do you want to make this edit to cart.py?","preview":"--- cart.py\n+++ cart.py\n@@ ...","preview_kind":"diff","always_label":"Yes, allow all edits during this session"}
→ {"id":4,"method":"permission.respond","params":{"request_id":"p1","answer":"yes"}}
← {"id":4,"result":{}}
← {"event":"tool.result","call_id":"c2","summary":"Updated cart.py with 1 addition and 1 removal","detail":"--- cart.py\n...","detail_kind":"diff",...}
...
← {"event":"assistant","text":"Fixed: ...",...}
← {"event":"turn.end","turn_id":"t1","checkpoint":{"turn_id":"t1","prompt":"fix the tests","files":["cart.py"],...}}
← {"event":"status","busy":false,...}
```
