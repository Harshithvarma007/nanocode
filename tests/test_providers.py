"""Provider tests: message translation, plus the real SDKs talking to a local mock server."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from nanocode.providers.anthropic_provider import AnthropicProvider, to_anthropic_messages
from nanocode.providers.ollama_provider import to_ollama_messages
from nanocode.providers.openai_provider import OpenAIProvider, to_openai_messages

HISTORY = [
    {"role": "user", "content": "fix it"},
    {"role": "assistant", "content": "Looking.", "tool_calls": [{"id": "call_1", "name": "read_file", "args": {"path": "a.py"}}]},
    {"role": "tool", "tool_call_id": "call_1", "name": "read_file", "content": "1\tx = 1", "is_error": False},
    {"role": "user", "content": "<system-reminder>plan mode off</system-reminder>"},
]


def test_anthropic_merges_tool_results_and_reminders_into_one_user_turn():
    out = to_anthropic_messages(HISTORY)
    assert [m["role"] for m in out] == ["user", "assistant", "user"]
    assert out[1]["content"][1] == {"type": "tool_use", "id": "call_1", "name": "read_file", "input": {"path": "a.py"}}
    assert [b["type"] for b in out[2]["content"]] == ["tool_result", "text"]  # tool_result must come first


def test_anthropic_replays_raw_blocks_verbatim():
    raw = [{"type": "thinking", "thinking": "hmm", "signature": "sig"}, {"type": "text", "text": "hi"}]
    out = to_anthropic_messages([{"role": "user", "content": "q"},
                                 {"role": "assistant", "content": "hi", "tool_calls": [], "raw": raw, "raw_kind": "anthropic"}])
    assert out[1]["content"] == raw


def test_openai_and_ollama_formats():
    oa = to_openai_messages(HISTORY)
    assert oa[1]["tool_calls"][0]["function"] == {"name": "read_file", "arguments": '{"path": "a.py"}'}
    assert oa[2] == {"role": "tool", "tool_call_id": "call_1", "content": "1\tx = 1"}
    ol = to_ollama_messages(HISTORY)
    assert ol[1]["tool_calls"][0]["function"]["arguments"] == {"path": "a.py"}
    assert ol[2]["tool_name"] == "read_file"


# --------------------------------------------------------------------------- mock server

class MockServer:
    def __init__(self, body: str, content_type="text/event-stream"):
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("content-length", 0))
                outer.requests.append({"path": self.path, "headers": dict(self.headers),
                                       "json": json.loads(self.rfile.read(length) or b"{}")})
                data = body.encode()
                self.send_response(200)
                self.send_header("content-type", content_type)
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()


def sse(events):
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)


ANTHROPIC_EVENTS = [
    {"type": "message_start", "message": {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
                                          "content": [], "stop_reason": None, "stop_sequence": None,
                                          "usage": {"input_tokens": 12, "output_tokens": 1,
                                                    "cache_read_input_tokens": 3000, "cache_creation_input_tokens": 100}}},
    {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "The bug is in add."}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "sig_abc"}},
    {"type": "content_block_stop", "index": 0},
    {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
    {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "Reading the file."}},
    {"type": "content_block_stop", "index": 1},
    {"type": "content_block_start", "index": 2, "content_block": {"type": "tool_use", "id": "toolu_1", "name": "read_file", "input": {}}},
    {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": "{\"path\": \"calc.py\"}"}},
    {"type": "content_block_stop", "index": 2},
    {"type": "message_delta", "delta": {"stop_reason": "tool_use", "stop_sequence": None}, "usage": {"output_tokens": 40}},
    {"type": "message_stop"},
]


@pytest.fixture
def anthropic_server():
    server = MockServer(sse(ANTHROPIC_EVENTS))
    yield server
    server.close()


def test_anthropic_provider_end_to_end(anthropic_server):
    provider = AnthropicProvider("claude-opus-5-5", api_key="test-key", base_url=anthropic_server.url)
    tools = [{"name": "read_file", "description": "Read a file", "parameters": {"type": "object", "properties": {}}}]
    events = list(provider.stream("SYSTEM", [{"role": "user", "content": "fix calc.py"}], tools))
    kinds = [k for k, _ in events]
    assert kinds[:3] == ["thinking", "text", "tool_start"] and kinds[-1] == "done"

    turn = events[-1][1]
    assert turn.text == "Reading the file." and turn.thinking == "The bug is in add."
    assert turn.tool_calls == [{"id": "toolu_1", "name": "read_file", "args": {"path": "calc.py"}}]
    assert turn.stop_reason == "tool_use"
    assert turn.usage.input_tokens == 12 + 3000 + 100 and turn.usage.cache_read_tokens == 3000
    assert [b["type"] for b in turn.raw] == ["thinking", "text", "tool_use"] and turn.raw[0]["signature"] == "sig_abc"

    request = anthropic_server.requests[0]
    body, betas = request["json"], request["headers"].get("anthropic-beta", "")
    assert body["model"] == "claude-opus-5-5" and body["stream"] is True and body["system"] == "SYSTEM"
    assert body["cache_control"] == {"type": "ephemeral"}
    assert body["thinking"] == {"type": "adaptive", "display": "summarized",
                                "block_binding": {"prefix_mismatch_behavior": "drop_block"}}
    assert body["output_config"] == {"effort": "high"} and body["fallbacks"] == "default"
    assert body["tools"][0]["eager_input_streaming"] is True and "input_schema" in body["tools"][0]
    assert "thinking-binding-controls-2026-08-01" in betas and "server-side-fallback-2026-07-01" in betas

    # The raw blocks replay on the next request exactly as received (signature included).
    follow_up = [{"role": "user", "content": "fix calc.py"}, turn.to_message(),
                 {"role": "tool", "tool_call_id": "toolu_1", "name": "read_file", "content": "ok", "is_error": False}]
    list(provider.stream("SYSTEM", follow_up, tools))
    replayed = anthropic_server.requests[1]["json"]["messages"][1]["content"]
    assert replayed[0] == {"type": "thinking", "thinking": "The bug is in add.", "signature": "sig_abc"}


def test_haiku_gets_no_fallbacks(anthropic_server):
    provider = AnthropicProvider("claude-haiku-5-5", api_key="test-key", base_url=anthropic_server.url)
    list(provider.stream("S", [{"role": "user", "content": "hi"}], []))
    body = anthropic_server.requests[0]["json"]
    assert "fallbacks" not in body and body["thinking"]["type"] == "adaptive"


def test_openai_provider_stitches_streamed_tool_calls():
    chunks = [
        {"choices": [{"index": 0, "delta": {"role": "assistant", "content": "Let me check."}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "call_9", "type": "function",
                                                            "function": {"name": "read_file", "arguments": ""}}]}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": "{\"path\":"}}]}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": " \"a.py\"}"}}]}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
        {"choices": [], "usage": {"prompt_tokens": 50, "completion_tokens": 9, "total_tokens": 59}},
    ]
    body = "".join("data: " + json.dumps({"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "m", **c}) + "\n\n"
                   for c in chunks) + "data: [DONE]\n\n"
    server = MockServer(body)
    try:
        provider = OpenAIProvider("m", base_url=server.url + "/v1", api_key="k")
        events = list(provider.stream("SYS", [{"role": "user", "content": "hi"}], []))
        turn = events[-1][1]
        assert turn.text == "Let me check." and turn.stop_reason == "tool_use"
        assert turn.tool_calls == [{"id": "call_9", "name": "read_file", "args": {"path": "a.py"}}]
        assert turn.usage.input_tokens == 50
        sent = server.requests[0]["json"]
        assert sent["messages"][0] == {"role": "system", "content": "SYS"} and sent["stream_options"] == {"include_usage": True}
    finally:
        server.close()
