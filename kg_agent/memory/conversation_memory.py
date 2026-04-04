from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]")


def _tokenize(text: str) -> set[str]:
    return {match.group(0).lower() for match in TOKEN_PATTERN.finditer(text or "")}


@dataclass
class MemoryMessage:
    role: str
    content: str
    timestamp: str
    session_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    user_id: str | None = None


class ConversationMemoryStore:
    def __init__(
        self,
        *,
        backend: str = "memory",
        sqlite_path: str | None = None,
        mongo_uri: str | None = None,
        mongo_database: str | None = None,
        mongo_collection: str = "kg_agent_conversation_messages",
        mongo_collection_handle: Any | None = None,
    ):
        self.backend = (backend or "memory").strip().lower() or "memory"
        self.sqlite_path = (sqlite_path or "").strip() or None
        self.mongo_uri = (
            (mongo_uri or "").strip()
            or os.getenv("MONGO_URI", "").strip()
            or os.getenv("MONGODB_URI", "").strip()
        )
        self.mongo_database = (
            (mongo_database or "").strip()
            or os.getenv("MONGO_DATABASE", "").strip()
            or "LightRAG"
        )
        self.mongo_collection_name = (
            (mongo_collection or "").strip() or "kg_agent_conversation_messages"
        )
        self._messages: dict[str, list[MemoryMessage]] = defaultdict(list)
        self._memory_lock = asyncio.Lock()
        self._init_lock = asyncio.Lock()
        self._sqlite_initialized = False
        self._mongo_collection = mongo_collection_handle
        self._mongo_client = None

    async def append_message(
        self,
        session_id: str,
        role: str,
        content: str,
        metadata: dict[str, Any] | None = None,
        user_id: str | None = None,
    ) -> MemoryMessage:
        message = MemoryMessage(
            session_id=session_id,
            role=role,
            content=content,
            timestamp=datetime.now(timezone.utc).isoformat(),
            metadata=metadata or {},
            user_id=(user_id or "").strip() or None,
        )
        if self.backend == "sqlite":
            async with self._memory_lock:
                await self._ensure_sqlite_initialized_locked()
                await asyncio.to_thread(
                    self._sqlite_insert_message,
                    session_id,
                    message,
                )
                return message
        if self.backend == "mongo":
            await self._ensure_mongo_initialized()
            await self._mongo_collection.insert_one(
                self._mongo_message_to_document(session_id, message)
            )
            return message
        async with self._memory_lock:
            self._messages[session_id].append(message)
        return message

    async def get_recent_history(
        self,
        session_id: str,
        turns: int = 6,
    ) -> list[dict[str, str]]:
        messages = await self._get_session_messages(session_id)
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
        messages = await self._get_session_messages(session_id)
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
        messages = await self._get_session_messages(session_id)
        return self._score_messages(messages, query=query, limit=limit)

    async def search_user_sessions(
        self,
        user_id: str | None,
        query: str,
        *,
        limit: int = 5,
        exclude_session_id: str | None = None,
    ) -> list[dict[str, Any]]:
        normalized_user_id = (user_id or "").strip()
        if not normalized_user_id:
            return []

        messages = await self._get_user_messages(
            normalized_user_id,
            exclude_session_id=exclude_session_id,
        )
        return self._score_messages(messages, query=query, limit=limit)

    async def clear_session(self, session_id: str) -> None:
        if self.backend == "sqlite":
            async with self._memory_lock:
                await self._ensure_sqlite_initialized_locked()
                await asyncio.to_thread(self._sqlite_clear_session, session_id)
                return
        if self.backend == "mongo":
            await self._ensure_mongo_initialized()
            result = self._mongo_collection.delete_many({"session_id": session_id})
            if asyncio.iscoroutine(result):
                await result
            return
        async with self._memory_lock:
            self._messages.pop(session_id, None)

    async def _get_session_messages(self, session_id: str) -> list[MemoryMessage]:
        if self.backend == "sqlite":
            async with self._memory_lock:
                await self._ensure_sqlite_initialized_locked()
                return await asyncio.to_thread(
                    self._sqlite_fetch_session_messages,
                    session_id,
                )
        if self.backend == "mongo":
            await self._ensure_mongo_initialized()
            cursor = self._mongo_collection.find({"session_id": session_id}).sort(
                [("_id", 1)]
            )
            rows = await cursor.to_list(length=None)
            return [self._mongo_row_to_message(row) for row in rows if isinstance(row, dict)]
        async with self._memory_lock:
            return list(self._messages.get(session_id, []))

    async def _get_user_messages(
        self,
        user_id: str,
        *,
        exclude_session_id: str | None = None,
    ) -> list[MemoryMessage]:
        if self.backend == "sqlite":
            async with self._memory_lock:
                await self._ensure_sqlite_initialized_locked()
                return await asyncio.to_thread(
                    self._sqlite_fetch_user_messages,
                    user_id,
                    exclude_session_id,
                )
        if self.backend == "mongo":
            await self._ensure_mongo_initialized()
            selector: dict[str, Any] = {"user_id": user_id}
            if exclude_session_id:
                selector["session_id"] = {"$ne": exclude_session_id}
            cursor = self._mongo_collection.find(selector).sort([("_id", 1)])
            rows = await cursor.to_list(length=None)
            return [self._mongo_row_to_message(row) for row in rows if isinstance(row, dict)]

        async with self._memory_lock:
            collected: list[MemoryMessage] = []
            for session_key, messages in self._messages.items():
                if exclude_session_id and session_key == exclude_session_id:
                    continue
                for message in messages:
                    if message.user_id == user_id:
                        collected.append(message)
            return collected

    @staticmethod
    def _score_messages(
        messages: list[MemoryMessage],
        *,
        query: str,
        limit: int,
    ) -> list[dict[str, Any]]:
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
                "session_id": message.session_id,
                "user_id": message.user_id,
            }
            for score, message in scored[:limit]
        ]

    async def _ensure_sqlite_initialized_locked(self) -> None:
        if self._sqlite_initialized:
            return
        if not self.sqlite_path:
            raise RuntimeError("ConversationMemoryStore sqlite backend requires sqlite_path")
        await asyncio.to_thread(self._sqlite_initialize)
        self._sqlite_initialized = True

    async def _ensure_mongo_initialized(self) -> None:
        if self._mongo_collection is not None:
            return
        async with self._init_lock:
            if self._mongo_collection is not None:
                return
            if not self.mongo_uri:
                raise RuntimeError(
                    "ConversationMemoryStore mongo backend requires MONGO_URI or MONGODB_URI"
                )
            try:
                from pymongo import AsyncMongoClient
            except ImportError as exc:
                raise RuntimeError(
                    "pymongo is required for ConversationMemoryStore mongo backend"
                ) from exc

            self._mongo_client = AsyncMongoClient(self.mongo_uri)
            database = self._mongo_client.get_database(self.mongo_database)
            self._mongo_collection = database.get_collection(self.mongo_collection_name)
            await self._mongo_collection.create_index([("session_id", 1), ("_id", 1)])
            await self._mongo_collection.create_index([("user_id", 1), ("_id", 1)])

    def _sqlite_initialize(self) -> None:
        path = Path(self.sqlite_path or "kg_agent_memory.sqlite3")
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    user_id TEXT,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    metadata_json TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_conversation_messages_session_id ON conversation_messages(session_id, id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_conversation_messages_user_id ON conversation_messages(user_id, id)"
            )
            conn.commit()

    def _sqlite_connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.sqlite_path or "kg_agent_memory.sqlite3")

    def _sqlite_insert_message(self, session_id: str, message: MemoryMessage) -> None:
        with self._sqlite_connect() as conn:
            conn.execute(
                """
                INSERT INTO conversation_messages (
                    session_id, user_id, role, content, timestamp, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    message.user_id,
                    message.role,
                    message.content,
                    message.timestamp,
                    json.dumps(message.metadata, ensure_ascii=False),
                ),
            )
            conn.commit()

    def _sqlite_fetch_session_messages(self, session_id: str) -> list[MemoryMessage]:
        with self._sqlite_connect() as conn:
            rows = conn.execute(
                """
                SELECT role, content, timestamp, metadata_json, user_id
                FROM conversation_messages
                WHERE session_id = ?
                ORDER BY id ASC
                """,
                (session_id,),
            ).fetchall()
        return [self._row_to_message(session_id, *row) for row in rows]

    def _sqlite_fetch_user_messages(
        self,
        user_id: str,
        exclude_session_id: str | None,
    ) -> list[MemoryMessage]:
        with self._sqlite_connect() as conn:
            if exclude_session_id:
                rows = conn.execute(
                    """
                    SELECT session_id, role, content, timestamp, metadata_json, user_id
                    FROM conversation_messages
                    WHERE user_id = ? AND session_id != ?
                    ORDER BY id ASC
                    """,
                    (user_id, exclude_session_id),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT session_id, role, content, timestamp, metadata_json, user_id
                    FROM conversation_messages
                    WHERE user_id = ?
                    ORDER BY id ASC
                    """,
                    (user_id,),
                ).fetchall()
        return [self._row_to_message(*row) for row in rows]

    def _sqlite_clear_session(self, session_id: str) -> None:
        with self._sqlite_connect() as conn:
            conn.execute(
                "DELETE FROM conversation_messages WHERE session_id = ?",
                (session_id,),
            )
            conn.commit()

    async def close(self) -> None:
        if self._mongo_client is not None:
            close_func = getattr(self._mongo_client, "close", None)
            if callable(close_func):
                result = close_func()
                if asyncio.iscoroutine(result):
                    await result
            self._mongo_client = None
        self._mongo_collection = None if self.backend == "mongo" else self._mongo_collection

    @staticmethod
    def _mongo_message_to_document(
        session_id: str,
        message: MemoryMessage,
    ) -> dict[str, Any]:
        metadata = message.metadata if isinstance(message.metadata, dict) else {}
        return {
            "session_id": session_id,
            "user_id": message.user_id,
            "role": message.role,
            "content": message.content,
            "timestamp": message.timestamp,
            "metadata": json.loads(json.dumps(metadata, ensure_ascii=False, default=str)),
        }

    @staticmethod
    def _mongo_row_to_message(row: dict[str, Any]) -> MemoryMessage:
        metadata = row.get("metadata", {})
        if not isinstance(metadata, dict):
            metadata = {}
        return MemoryMessage(
            session_id=(str(row.get("session_id") or "").strip() or None),
            role=str(row.get("role") or ""),
            content=str(row.get("content") or ""),
            timestamp=str(row.get("timestamp") or ""),
            metadata=metadata,
            user_id=(str(row.get("user_id") or "").strip() or None),
        )

    @staticmethod
    def _row_to_message(
        session_id: str | None,
        role: str,
        content: str,
        timestamp: str,
        metadata_json: str,
        user_id: str | None,
    ) -> MemoryMessage:
        try:
            metadata = json.loads(metadata_json or "{}")
        except json.JSONDecodeError:
            metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        return MemoryMessage(
            session_id=(session_id or "").strip() or None,
            role=role,
            content=content,
            timestamp=timestamp,
            metadata=metadata,
            user_id=(user_id or "").strip() or None,
        )
