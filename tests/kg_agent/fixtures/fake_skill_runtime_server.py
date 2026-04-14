from __future__ import annotations

import json
import sys
from copy import deepcopy


DEFAULT_RUNTIME_TARGET = {
    "platform": "linux",
    "shell": "/bin/sh",
    "workspace_root": "/workspace",
    "workdir": "/workspace",
    "network_allowed": False,
    "supports_python": True,
}
DEFAULT_PREFLIGHT = {
    "status": "ok",
    "ok": True,
    "failure_reason": None,
    "required_tools": [],
    "generated_files": [],
}
RUN_RESULTS: dict[str, dict] = {}


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


def _default_command_plan(shell_command: str) -> dict:
    return {
        "skill_name": "example-skill",
        "goal": "Use example skill",
        "user_query": "run example skill",
        "constraints": {},
        "command": shell_command,
        "mode": "explicit",
        "shell_mode": "conservative",
        "runtime_target": deepcopy(DEFAULT_RUNTIME_TARGET),
        "rationale": "stub",
        "entrypoint": None,
        "cli_args": [],
        "missing_fields": [],
        "failure_reason": None,
        "hints": {},
    }


def _build_completed_payload(*, arguments: dict, run_id: str) -> dict:
    constraints = arguments.get("constraints", {})
    skill_name = str(arguments.get("skill_name", "example-skill") or "example-skill")
    command_plan = (
        deepcopy(arguments.get("command_plan"))
        if isinstance(arguments.get("command_plan"), dict)
        else {}
    )
    shell_command = (
        constraints.get("shell_command") if isinstance(constraints, dict) else None
    ) or (
        command_plan.get("command")
        if isinstance(command_plan.get("command"), str)
        else None
    ) or "python scripts/run_report.py --topic 'stub'"
    if not command_plan:
        command_plan = _default_command_plan(shell_command)
    command_plan.setdefault("command", shell_command)
    command_plan.setdefault("mode", "explicit")
    command_plan.setdefault("shell_mode", "conservative")
    command_plan.setdefault("runtime_target", deepcopy(DEFAULT_RUNTIME_TARGET))
    command_plan.setdefault("constraints", {})
    command_plan.setdefault("cli_args", [])
    command_plan.setdefault("missing_fields", [])
    command_plan.setdefault("failure_reason", None)
    command_plan.setdefault("hints", {})
    return {
        "summary": "shell runtime executed",
        "skill_name": skill_name,
        "run_status": "completed",
        "status": "completed",
        "success": True,
        "execution_mode": "shell",
        "run_id": run_id,
        "command": shell_command,
        "shell_mode": command_plan["shell_mode"],
        "runtime_target": deepcopy(command_plan["runtime_target"]),
        "workspace": f"/workspace/{run_id}",
        "started_at": "2026-01-01T00:00:00+00:00",
        "finished_at": "2026-01-01T00:00:01+00:00",
        "exit_code": 0,
        "failure_reason": None,
        "command_plan": deepcopy(command_plan),
        "preflight": deepcopy(DEFAULT_PREFLIGHT),
        "repair_attempted": False,
        "repair_succeeded": False,
        "repaired_from_run_id": None,
        "cancel_requested": False,
        "runtime": {
            "executor": "shell",
            "delivery": "durable_worker",
            "queue_state": "completed",
            "store_backend": "sqlite",
            "cancel_supported": True,
            "log_streaming": True,
        },
        "artifacts": [{"path": "report.md", "size_bytes": 128}],
        "logs_preview": {
            "stdout": "report generated",
            "stderr": "",
            "stdout_truncated": False,
            "stderr_truncated": False,
        },
        "logs": {"stdout": "report generated", "stderr": ""},
        "echo": arguments,
    }


def _build_running_payload(completed_payload: dict) -> dict:
    running_payload = deepcopy(completed_payload)
    running_payload.update(
        {
            "summary": "shell runtime running",
            "run_status": "running",
            "status": "running",
            "finished_at": None,
            "exit_code": None,
            "artifacts": [],
            "runtime": {
                **deepcopy(completed_payload.get("runtime", {})),
                "queue_state": "queued",
            },
            "logs_preview": {
                "stdout": "",
                "stderr": "",
                "stdout_truncated": False,
                "stderr_truncated": False,
            },
            "logs": {"stdout": "", "stderr": ""},
            "cancel_requested": False,
        }
    )
    return running_payload


def _build_cancelled_payload(completed_payload: dict) -> dict:
    cancelled_payload = deepcopy(completed_payload)
    cancelled_payload.update(
        {
            "summary": "shell runtime cancelled",
            "run_status": "failed",
            "status": "failed",
            "success": False,
            "finished_at": "2026-01-01T00:00:00+00:00",
            "failure_reason": "cancelled",
            "cancel_requested": True,
            "runtime": {
                **deepcopy(completed_payload.get("runtime", {})),
                "queue_state": "failed",
            },
            "logs_preview": {
                "stdout": "cancel requested",
                "stderr": "",
                "stdout_truncated": False,
                "stderr_truncated": False,
            },
            "logs": {"stdout": "cancel requested", "stderr": ""},
            "artifacts": [],
        }
    )
    return cancelled_payload


def _load_completed_run(run_id: str) -> dict | None:
    if run_id in RUN_RESULTS:
        return deepcopy(RUN_RESULTS[run_id])
    if run_id != "skill-run-123":
        return None
    payload = _build_completed_payload(arguments={}, run_id=run_id)
    RUN_RESULTS[run_id] = deepcopy(payload)
    return payload


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
            run_id = "skill-run-123"
            completed_payload = _build_completed_payload(arguments=arguments, run_id=run_id)
            RUN_RESULTS[run_id] = deepcopy(completed_payload)
            constraints = arguments.get("constraints", {})
            wait_for_completion = bool(arguments.get("wait_for_completion")) or (
                isinstance(constraints, dict) and bool(constraints.get("wait_for_completion"))
            )
            payload = (
                completed_payload
                if wait_for_completion
                else _build_running_payload(completed_payload)
            )
            _write_message(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "content": [{"type": "text", "text": payload["summary"]}],
                        "structuredContent": payload,
                        "isError": False,
                    },
                }
            )
            continue
        if tool_name == "get_run_status":
            run_id = arguments.get("run_id", "")
            payload = _load_completed_run(run_id)
            if payload is None:
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
            payload["summary"] = "loaded run status"
            _write_message(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "content": [{"type": "text", "text": "loaded run status"}],
                        "structuredContent": payload,
                        "isError": False,
                    },
                }
            )
            continue
        if tool_name == "get_run_logs":
            run_id = arguments.get("run_id", "")
            payload = _load_completed_run(run_id)
            if payload is None:
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
            logs_payload = {
                "summary": "loaded logs",
                "run_id": run_id,
                "skill_name": payload["skill_name"],
                "run_status": payload["run_status"],
                "status": payload["status"],
                "success": payload["success"],
                "command": payload["command"],
                "shell_mode": payload["shell_mode"],
                "runtime_target": deepcopy(payload["runtime_target"]),
                "started_at": payload["started_at"],
                "finished_at": payload["finished_at"],
                "failure_reason": payload["failure_reason"],
                "runtime": deepcopy(payload.get("runtime", {})),
                "preflight": deepcopy(payload["preflight"]),
                "repair_attempted": payload["repair_attempted"],
                "repair_succeeded": payload["repair_succeeded"],
                "repaired_from_run_id": payload["repaired_from_run_id"],
                "cancel_requested": payload.get("cancel_requested", False),
                "stdout": payload["logs"]["stdout"],
                "stderr": payload["logs"]["stderr"],
            }
            _write_message(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "content": [{"type": "text", "text": "loaded logs"}],
                        "structuredContent": logs_payload,
                        "isError": False,
                    },
                }
            )
            continue
        if tool_name == "get_run_artifacts":
            run_id = arguments.get("run_id", "")
            payload = _load_completed_run(run_id)
            if payload is None:
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
            artifacts_payload = {
                "summary": "loaded artifacts",
                "run_id": run_id,
                "skill_name": payload["skill_name"],
                "run_status": payload["run_status"],
                "status": payload["status"],
                "success": payload["success"],
                "shell_mode": payload["shell_mode"],
                "runtime_target": deepcopy(payload["runtime_target"]),
                "workspace": payload["workspace"],
                "started_at": payload["started_at"],
                "finished_at": payload["finished_at"],
                "failure_reason": payload["failure_reason"],
                "runtime": deepcopy(payload.get("runtime", {})),
                "preflight": deepcopy(payload["preflight"]),
                "repair_attempted": payload["repair_attempted"],
                "repair_succeeded": payload["repair_succeeded"],
                "repaired_from_run_id": payload["repaired_from_run_id"],
                "cancel_requested": payload.get("cancel_requested", False),
                "artifacts": deepcopy(payload["artifacts"]),
            }
            _write_message(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "content": [{"type": "text", "text": "loaded artifacts"}],
                        "structuredContent": artifacts_payload,
                        "isError": False,
                    },
                }
            )
            continue
        if tool_name == "cancel_skill_run":
            run_id = arguments.get("run_id", "")
            payload = _load_completed_run(run_id)
            if payload is None:
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
            cancelled_payload = _build_cancelled_payload(payload)
            RUN_RESULTS[run_id] = deepcopy(cancelled_payload)
            _write_message(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "content": [{"type": "text", "text": "cancelled run"}],
                        "structuredContent": cancelled_payload,
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
