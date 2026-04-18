from __future__ import annotations

import os
import shlex
from datetime import datetime, timezone


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def truncate_log(text: str, *, max_bytes: int) -> tuple[str, bool]:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text, False
    return encoded[:max_bytes].decode("utf-8", errors="ignore"), True


def truncate_utf8_text(text: str, max_bytes: int) -> tuple[str, bool]:
    if max_bytes <= 0:
        return "", bool(text)
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text, False
    return encoded[:max_bytes].decode("utf-8", errors="ignore"), True


def utc_duration_seconds(
    started_at: str | None,
    finished_at: str | None,
) -> float | None:
    if not started_at or not finished_at:
        return None
    try:
        started_dt = datetime.fromisoformat(str(started_at))
        finished_dt = datetime.fromisoformat(str(finished_at))
    except ValueError:
        return None
    return max(0.0, (finished_dt - started_dt).total_seconds())


def powershell_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def shell_join(argv: list[str]) -> str:
    normalized = [str(item) for item in argv if str(item)]
    if not normalized:
        return ""
    if os.name == "nt":
        return "& " + " ".join(powershell_quote(item) for item in normalized)
    return shlex.join(normalized)


def build_shell_exec_argv(shell_command: str) -> list[str]:
    if os.name == "nt":
        return ["powershell.exe", "-NoProfile", "-Command", shell_command]
    return ["/bin/sh", "-c", shell_command]
