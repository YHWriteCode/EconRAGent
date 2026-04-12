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
                    "serverInfo": {"name": "fake-skill-runtime", "version": "0.1.0"},
                },
            }
        )
        continue
    if method == "tools/list":
        _write_message(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"tools": []},
            }
        )
        continue
    if method == "tools/call":
        params = message.get("params", {})
        arguments = params.get("arguments", {})
        tool_name = params.get("name", "")
        if tool_name == "run_skill_task":
            constraints = arguments.get("constraints", {})
            shell_command = (
                constraints.get("shell_command")
                if isinstance(constraints, dict)
                else None
            ) or "python scripts/run_report.py --topic 'stub'"
            payload = {
                "summary": "shell runtime executed",
                "status": "completed",
                "success": True,
                "execution_mode": "shell",
                "run_id": "skill-run-123",
                "command": shell_command,
                "workspace": "/workspace/skill-run-123",
                "exit_code": 0,
                "artifacts": [{"path": "report.md", "size_bytes": 128}],
                "logs_preview": {
                    "stdout": "report generated",
                    "stderr": "",
                    "stdout_truncated": False,
                    "stderr_truncated": False,
                },
                "echo": arguments,
            }
            _write_message(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "content": [{"type": "text", "text": "shell runtime executed"}],
                        "structuredContent": payload,
                        "isError": False,
                    },
                }
            )
            continue
        if tool_name == "get_run_logs":
            run_id = arguments.get("run_id", "")
            if run_id != "skill-run-123":
                _write_message(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {
                            "content": [{"type": "text", "text": f"Unknown run_id: {run_id}"}],
                            "structuredContent": {"summary": f"Unknown run_id: {run_id}"},
                            "isError": True,
                        },
                    }
                )
                continue
            payload = {
                "summary": "loaded logs",
                "run_id": run_id,
                "skill_name": "example-skill",
                "status": "completed",
                "success": True,
                "command": "python scripts/run_report.py --topic 'stub'",
                "stdout": "report generated",
                "stderr": "",
            }
            _write_message(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "content": [{"type": "text", "text": "loaded logs"}],
                        "structuredContent": payload,
                        "isError": False,
                    },
                }
            )
            continue
        if tool_name == "get_run_artifacts":
            run_id = arguments.get("run_id", "")
            if run_id != "skill-run-123":
                _write_message(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {
                            "content": [{"type": "text", "text": f"Unknown run_id: {run_id}"}],
                            "structuredContent": {"summary": f"Unknown run_id: {run_id}"},
                            "isError": True,
                        },
                    }
                )
                continue
            payload = {
                "summary": "loaded artifacts",
                "run_id": run_id,
                "skill_name": "example-skill",
                "status": "completed",
                "success": True,
                "workspace": "/workspace/skill-run-123",
                "artifacts": [{"path": "report.md", "size_bytes": 128}],
            }
            _write_message(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "content": [{"type": "text", "text": "loaded artifacts"}],
                        "structuredContent": payload,
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
                    "content": [{"type": "text", "text": f"Unhandled tool: {tool_name}"}],
                    "structuredContent": {"summary": f"Unhandled tool: {tool_name}"},
                    "isError": True,
                },
            }
        )
        continue
