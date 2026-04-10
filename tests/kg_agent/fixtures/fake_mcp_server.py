from __future__ import annotations

import json
import sys


def _read_message():
    content_length = None
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        if line in {b"\r\n", b"\n"}:
            break
        decoded = line.decode("ascii", errors="ignore").strip()
        if decoded.lower().startswith("content-length:"):
            content_length = int(decoded.split(":", 1)[1].strip())
    if content_length is None:
        return None
    payload = sys.stdin.buffer.read(content_length)
    return json.loads(payload.decode("utf-8"))


def _write_message(message):
    encoded = json.dumps(message, ensure_ascii=False).encode("utf-8")
    header = f"Content-Length: {len(encoded)}\r\n\r\n".encode("ascii")
    sys.stdout.buffer.write(header + encoded)
    sys.stdout.buffer.flush()


while True:
    message = _read_message()
    if message is None:
        break
    method = message.get("method")
    request_id = message.get("id")
    if request_id is None:
        continue
    if method == "initialize":
        _write_message(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "fake-mcp", "version": "0.1.0"},
                },
            }
        )
        continue
    if method == "tools/list":
        _write_message(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "tools": [
                        {
                            "name": "quant_backtest",
                            "description": "Run a backtest through fake MCP.",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "query": {"type": "string"},
                                    "symbol": {"type": "string"},
                                },
                                "required": ["query"],
                            },
                        },
                        {
                            "name": "portfolio_stats",
                            "description": "Summarize portfolio metrics through fake MCP.",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "query": {"type": "string"},
                                    "portfolio_id": {"type": "string"},
                                },
                            },
                        },
                    ]
                },
            }
        )
        continue
    if method == "tools/call":
        params = message.get("params", {})
        arguments = params.get("arguments", {})
        tool_name = params.get("name", "")
        _write_message(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": f"{tool_name or 'tool'} completed by fake MCP",
                        }
                    ],
                    "structuredContent": {
                        "summary": f"{tool_name or 'tool'} completed by fake MCP",
                        "tool": tool_name,
                        "echo": arguments,
                    },
                    "isError": False,
                },
            }
        )
        continue
    _write_message(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "content": [{"type": "text", "text": f"Unhandled method: {method}"}],
                "structuredContent": {"summary": f"Unhandled method: {method}"},
                "isError": True,
            },
        }
    )
