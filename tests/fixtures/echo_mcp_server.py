"""A tiny MCP server for tests: newline-delimited JSON-RPC 2.0 on stdin/stdout."""

import json
import sys

TOOLS = [
    {"name": "echo", "description": "Echo text back",
     "inputSchema": {"$schema": "http://json-schema.org/draft-07/schema#", "type": "object",
                     "properties": {"text": {"type": "string"}}, "required": ["text"]}},
    {"name": "add", "description": "Add two numbers",
     "inputSchema": {"type": "object", "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
                     "required": ["a", "b"]}},
]


def send(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


print("echo server starting", file=sys.stderr)
for line in sys.stdin:
    msg = json.loads(line)
    if "id" not in msg:
        continue  # a notification
    method, rid = msg["method"], msg["id"]
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                                                       "serverInfo": {"name": "echo", "version": "1.0"}}})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}})
    elif method == "tools/call":
        name, args = msg["params"]["name"], msg["params"].get("arguments", {})
        if name == "echo":
            content, error = args["text"], False
        elif name == "add":
            content, error = str(args["a"] + args["b"]), False
        else:
            content, error = f"no tool {name}", True
        send({"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": content}], "isError": error}})
    else:
        send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"unknown method {method}"}})
