from __future__ import annotations

import asyncio
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]")


def _tokenize(text: str) -> set[str]:
    return {match.group(0).lower() for match in TOKEN_PATTERN.finditer(text or "")}


@dataclass
class MemoryMessage:
    role: str
    content: str
    timestamp: str
    metadata: dict[str, Any] = field(default_factory=dict)


class ConversationMemoryStore:
    def __init__(self):
        self._messages: dict[str, list[MemoryMessage]] = defaultdict(list)
        self._lock = asyncio.Lock()

    async def append_message(
        self,
        session_id: str,
        role: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        message = MemoryMessage(
            role=role,
            content=content,
            timestamp=datetime.now(timezone.utc).isoformat(),
            metadata=metadata or {},
        )
        async with self._lock:
            self._messages[session_id].append(message)

    async def get_recent_history(
        self,
        session_id: str,
        turns: int = 6,
    ) -> list[dict[str, str]]:
        async with self._lock:
            messages = list(self._messages.get(session_id, []))
        if turns <= 0:
            return []
        slice_size = max(turns * 2, turns)
        return [
            {"role": message.role, "content": message.content}
            for message in messages[-slice_size:]
        ]

    async def get_recent_tool_calls(
        self,
        session_id: str,
        assistant_turns: int = 2,
    ) -> list[dict[str, Any]]:
        async with self._lock:
            messages = list(self._messages.get(session_id, []))
        if assistant_turns <= 0:
            return []

        collected: list[list[dict[str, Any]]] = []
        seen_assistant_messages = 0
        for message in reversed(messages):
            if message.role != "assistant":
                continue
            compact_tool_calls = message.metadata.get("compact_tool_calls")
            if not isinstance(compact_tool_calls, list):
                continue
            normalized = [
                dict(item)
                for item in compact_tool_calls
                if isinstance(item, dict)
            ]
            if not normalized:
                continue
            collected.append(normalized)
            seen_assistant_messages += 1
            if seen_assistant_messages >= assistant_turns:
                break

        flattened: list[dict[str, Any]] = []
        for group in reversed(collected):
            flattened.extend(group)
        return flattened

    async def search(
        self,
        session_id: str,
        query: str,
        limit: int = 4,
    ) -> list[dict[str, Any]]:
        async with self._lock:
            messages = list(self._messages.get(session_id, []))
        if not messages:
            return []

        query_tokens = _tokenize(query)
        scored: list[tuple[int, MemoryMessage]] = []
        for message in messages:
            score = len(query_tokens & _tokenize(message.content))
            if score > 0 or not query_tokens:
                scored.append((score, message))

        scored.sort(key=lambda item: (item[0], item[1].timestamp), reverse=True)
        return [
            {
                "role": message.role,
                "content": message.content,
                "timestamp": message.timestamp,
                "score": score,
                "metadata": message.metadata,
            }
            for score, message in scored[:limit]
        ]

    async def clear_session(self, session_id: str) -> None:
        async with self._lock:
            self._messages.pop(session_id, None)
