from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class UserProfile:
    user_id: str
    attributes: dict[str, Any] = field(default_factory=dict)


class UserProfileStore:
    def __init__(
        self,
        *,
        backend: str = "memory",
        sqlite_path: str | None = None,
        mongo_uri: str | None = None,
        mongo_database: str | None = None,
        mongo_collection: str = "kg_agent_user_profiles",
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
            (mongo_collection or "").strip() or "kg_agent_user_profiles"
        )
        self._profiles: dict[str, UserProfile] = {}
        self._memory_lock = asyncio.Lock()
        self._init_lock = asyncio.Lock()
        self._sqlite_initialized = False
        self._mongo_collection = mongo_collection_handle
        self._mongo_client = None

    async def get_profile(self, user_id: str | None) -> dict[str, Any]:
        normalized_user_id = (user_id or "").strip()
        if not normalized_user_id:
            return {}
        if self.backend == "sqlite":
            async with self._memory_lock:
                await self._ensure_sqlite_initialized_locked()
                return await asyncio.to_thread(
                    self._sqlite_get_profile,
                    normalized_user_id,
                )
        if self.backend == "mongo":
            await self._ensure_mongo_initialized()
            document = await self._mongo_collection.find_one({"user_id": normalized_user_id})
            if not isinstance(document, dict):
                return {}
            attributes = document.get("attributes", {})
            return dict(attributes) if isinstance(attributes, dict) else {}
        async with self._memory_lock:
            profile = self._profiles.get(normalized_user_id)
        return dict(profile.attributes) if profile else {}

    async def update_profile(
        self, user_id: str | None, attributes: dict[str, Any]
    ) -> None:
        normalized_user_id = (user_id or "").strip()
        if not normalized_user_id:
            return
        if self.backend == "sqlite":
            async with self._memory_lock:
                await self._ensure_sqlite_initialized_locked()
                await asyncio.to_thread(
                    self._sqlite_update_profile,
                    normalized_user_id,
                    attributes,
                )
                return
        if self.backend == "mongo":
            await self._ensure_mongo_initialized()
            safe_attributes = json.loads(
                json.dumps(attributes or {}, ensure_ascii=False, default=str)
            )
            if not isinstance(safe_attributes, dict) or not safe_attributes:
                return
            if all(
                isinstance(key, str) and key and "." not in key and not key.startswith("$")
                for key in safe_attributes
            ):
                update_result = self._mongo_collection.update_one(
                    {"user_id": normalized_user_id},
                    {
                        "$set": {
                            **{
                                f"attributes.{key}": value
                                for key, value in safe_attributes.items()
                            },
                            "user_id": normalized_user_id,
                        }
                    },
                    upsert=True,
                )
                if asyncio.iscoroutine(update_result):
                    await update_result
                return
            current = await self.get_profile(normalized_user_id)
            current.update(safe_attributes)
            replace_result = self._mongo_collection.replace_one(
                {"user_id": normalized_user_id},
                {"user_id": normalized_user_id, "attributes": current},
                upsert=True,
            )
            if asyncio.iscoroutine(replace_result):
                await replace_result
            return
        async with self._memory_lock:
            profile = self._profiles.setdefault(
                normalized_user_id,
                UserProfile(user_id=normalized_user_id),
            )
            profile.attributes.update(attributes)

    async def _ensure_sqlite_initialized_locked(self) -> None:
        if self._sqlite_initialized:
            return
        if not self.sqlite_path:
            raise RuntimeError("UserProfileStore sqlite backend requires sqlite_path")
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
                    "UserProfileStore mongo backend requires MONGO_URI or MONGODB_URI"
                )
            try:
                from pymongo import AsyncMongoClient
            except ImportError as exc:
                raise RuntimeError(
                    "pymongo is required for UserProfileStore mongo backend"
                ) from exc

            self._mongo_client = AsyncMongoClient(self.mongo_uri)
            database = self._mongo_client.get_database(self.mongo_database)
            self._mongo_collection = database.get_collection(self.mongo_collection_name)
            await self._mongo_collection.create_index([("user_id", 1)])

    def _sqlite_initialize(self) -> None:
        path = Path(self.sqlite_path or "kg_agent_profiles.sqlite3")
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS user_profiles (
                    user_id TEXT PRIMARY KEY,
                    attributes_json TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def _sqlite_connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.sqlite_path or "kg_agent_profiles.sqlite3")

    def _sqlite_get_profile(self, user_id: str) -> dict[str, Any]:
        with self._sqlite_connect() as conn:
            row = conn.execute(
                "SELECT attributes_json FROM user_profiles WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if row is None:
            return {}
        try:
            payload = json.loads(row[0] or "{}")
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _sqlite_update_profile(
        self,
        user_id: str,
        attributes: dict[str, Any],
    ) -> None:
        current = self._sqlite_get_profile(user_id)
        current.update(attributes)
        with self._sqlite_connect() as conn:
            conn.execute(
                """
                INSERT INTO user_profiles(user_id, attributes_json)
                VALUES (?, ?)
                ON CONFLICT(user_id) DO UPDATE SET attributes_json = excluded.attributes_json
                """,
                (user_id, json.dumps(current, ensure_ascii=False)),
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
