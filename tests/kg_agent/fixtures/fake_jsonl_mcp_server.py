from __future__ import annotations

import json
import sys


def _read_message():
    line = sys.stdin.buffer.readline()
    if not line:
        return None
    return json.loads(line.decode("utf-8"))


def _write_message(message):
    encoded = json.dumps(message, ensure_ascii=False).encode("utf-8")
    sys.stdout.buffer.write(encoded + b"\n")
    sys.stdout.buffer.flush()


def main():
    while True:
        message = _read_message()
        if message is None:
            break
        message_id = message.get("id")
        method = message.get("method")

        if method == "initialize":
            _write_message(
                {
                    "jsonrpc": "2.0",
                    "id": message_id,
                    "result": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "fake-jsonl-mcp", "version": "0.1.0"},
                    },
                }
            )
            continue

        if method == "notifications/initialized":
            continue

        if method == "tools/call":
            params = message.get("params", {})
            arguments = params.get("arguments", {})
            payload_bytes = int(arguments.get("payload_bytes", 0) or 0)
            _write_message(
                {
                    "jsonrpc": "2.0",
                    "id": message_id,
                    "result": {
                        "content": [
                            {
                                "type": "text",
                                "text": "json-lines skill runtime executed",
                            }
                        ],
                        "structuredContent": {
                            "tool": params.get("name"),
                            "echo": arguments,
                            "status": "completed",
                            "blob": ("x" * payload_bytes) if payload_bytes > 0 else "",
                        },
                        "isError": False,
                    },
                }
            )
            continue

        _write_message(
            {
                "jsonrpc": "2.0",
                "id": message_id,
                "error": {"code": -32601, "message": f"Unknown method: {method}"},
            }
        )


if __name__ == "__main__":
    main()
