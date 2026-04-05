import asyncio
from pathlib import Path

import numpy as np
import pytest

from kg_agent.memory.conversation_memory import ConversationMemoryStore, MemoryMessage
from kg_agent.memory.cross_session_store import CrossSessionStore
from kg_agent.memory.user_profile import UserProfileStore


class _FakeEmbeddingFunc:
    embedding_dim = 3
    model_name = "test-embed"

    async def __call__(self, texts, **kwargs):
        vectors = []
        for text in texts:
            lowered = (text or "").lower()
            vectors.append(
                [
                    1.0 if "supplier" in lowered else 0.0,
                    1.0 if "logistics" in lowered else 0.0,
                    1.0 if "battery" in lowered else 0.0,
                ]
            )
        return np.array(vectors, dtype=float)


class _FakeMongoCursor:
    def __init__(self, rows):
        self.rows = list(rows)
        self._limit = None

    def sort(self, spec):
        fields = spec if isinstance(spec, list) else [spec]
        for field_name, direction in reversed(fields):
            reverse = int(direction) < 0
            self.rows.sort(
                key=lambda row: row.get(field_name) if isinstance(row, dict) else None,
                reverse=reverse,
            )
        return self

    def limit(self, value):
        self._limit = value
        return self

    async def to_list(self, length=None):
        effective_length = self._limit if length is None else length
        if effective_length is None:
            return list(self.rows)
        return list(self.rows)[:effective_length]


class _FakeMongoCollection:
    def __init__(self):
        self.documents: dict[str, dict] = {}
        self.indexes: list[tuple] = []
        self._next_identifier = 1

    async def create_index(self, spec, **kwargs):
        self.indexes.append(tuple(spec))

    async def find_one(self, selector):
        for row in self.documents.values():
            if _mongo_match_selector(row, selector):
                return dict(row)
        return None

    async def replace_one(self, selector, document, upsert=False):
        for identifier, row in list(self.documents.items()):
            if _mongo_match_selector(row, selector):
                payload = dict(document)
                payload.setdefault("_id", row.get("_id", identifier))
                self.documents[str(payload["_id"])] = payload
                if str(payload["_id"]) != identifier:
                    self.documents.pop(identifier, None)
                return
        if upsert:
            payload = dict(document)
            payload.setdefault("_id", selector.get("_id", self._allocate_id()))
            self.documents[str(payload["_id"])] = payload

    async def insert_one(self, document):
        payload = dict(document)
        payload.setdefault("_id", self._allocate_id())
        self.documents[str(payload["_id"])] = payload
        return type("InsertOneResult", (), {"inserted_id": payload["_id"]})()

    async def delete_one(self, selector):
        for identifier, row in list(self.documents.items()):
            if _mongo_match_selector(row, selector):
                self.documents.pop(identifier, None)
                break

    def delete_many(self, selector):
        removed = 0
        for identifier, row in list(self.documents.items()):
            if _mongo_match_selector(row, selector):
                self.documents.pop(identifier, None)
                removed += 1
        return type("DeleteResult", (), {"deleted_count": removed})()

    def update_one(self, selector, update, upsert=False):
        target_id = None
        target = None
        for identifier, row in self.documents.items():
            if _mongo_match_selector(row, selector):
                target_id = identifier
                target = dict(row)
                break
        if target is None and not upsert:
            return type("UpdateResult", (), {"matched_count": 0, "modified_count": 0})()
        if target is None:
            target = {}
            target_id = str(selector.get("_id", self._allocate_id()))
        for key, value in (update.get("$setOnInsert", {}) or {}).items():
            if key not in target:
                _mongo_assign_field(target, key, value)
        for key, value in (update.get("$set", {}) or {}).items():
            _mongo_assign_field(target, key, value)
        target.setdefault("_id", target_id)
        self.documents[str(target["_id"])] = target
        return type("UpdateResult", (), {"matched_count": 1, "modified_count": 1})()

    def find(self, selector):
        if not selector:
            rows = list(self.documents.values())
            return _FakeMongoCursor(rows)
        rows = [
            row
            for row in self.documents.values()
            if _mongo_match_selector(row, selector)
        ]
        return _FakeMongoCursor(rows)

    def _allocate_id(self):
        identifier = self._next_identifier
        self._next_identifier += 1
        return identifier


class _FakeQdrantClient:
    def __init__(self):
        self.collections: dict[str, dict[str, object]] = {}

    def collection_exists(self, collection_name):
        return collection_name in self.collections

    def create_collection(self, collection_name, vectors_config):
        self.collections.setdefault(collection_name, {})

    def create_payload_index(self, **kwargs):
        return None

    def upsert(self, collection_name, points, wait=True):
        bucket = self.collections.setdefault(collection_name, {})
        for point in points:
            bucket[str(point.id)] = point

    def delete(self, collection_name, points_selector, wait=True):
        bucket = self.collections.setdefault(collection_name, {})
        for point_id in getattr(points_selector, "points", []) or []:
            bucket.pop(str(point_id), None)

    def query_points(
        self,
        collection_name,
        query,
        limit,
        with_payload=True,
        query_filter=None,
    ):
        bucket = self.collections.get(collection_name, {})
        scored = []
        for point in bucket.values():
            payload = getattr(point, "payload", {}) or {}
            if not _match_filter(payload, query_filter):
                continue
            vector = getattr(point, "vector", [])
            if hasattr(vector, "tolist"):
                vector = vector.tolist()
            numerator = sum(float(a) * float(b) for a, b in zip(vector, query))
            vector_norm = sum(float(a) * float(a) for a in vector) ** 0.5
            query_norm = sum(float(a) * float(a) for a in query) ** 0.5
            score = 0.0
            if vector_norm > 0 and query_norm > 0:
                score = numerator / (vector_norm * query_norm)
            scored.append(
                type(
                    "QdrantPoint",
                    (),
                    {
                        "id": point.id,
                        "payload": payload,
                        "score": score,
                    },
                )()
            )
        scored.sort(key=lambda item: item.score, reverse=True)
        return type("QueryResult", (), {"points": scored[:limit]})()


def _match_filter(payload, query_filter) -> bool:
    if query_filter is None:
        return True
    for condition in getattr(query_filter, "must", []) or []:
        key = getattr(condition, "key", "")
        match = getattr(condition, "match", None)
        value = getattr(match, "value", None)
        payload_value = payload.get(key)
        if isinstance(payload_value, list):
            if value not in payload_value:
                return False
            continue
        if payload_value != value:
            return False
    for condition in getattr(query_filter, "must_not", []) or []:
        key = getattr(condition, "key", "")
        match = getattr(condition, "match", None)
        value = getattr(match, "value", None)
        payload_value = payload.get(key)
        if isinstance(payload_value, list):
            if value in payload_value:
                return False
            continue
        if payload_value == value:
            return False
    return True


def _mongo_match_selector(row, selector) -> bool:
    for key, value in (selector or {}).items():
        field_value = row.get(key)
        if isinstance(value, dict):
            if "$in" in value and field_value not in value["$in"]:
                return False
            if "$ne" in value and field_value == value["$ne"]:
                return False
            continue
        if field_value != value:
            return False
    return True


def _mongo_assign_field(target, dotted_key, value):
    parts = [part for part in str(dotted_key).split(".") if part]
    if not parts:
        return
    current = target
    for part in parts[:-1]:
        child = current.get(part)
        if not isinstance(child, dict):
            child = {}
            current[part] = child
        current = child
    current[parts[-1]] = value


@pytest.mark.asyncio
async def test_conversation_memory_appends_and_returns_recent_history():
    store = ConversationMemoryStore()
    await store.append_message("s1", "user", "What is BYD?")
    await store.append_message("s1", "assistant", "BYD is an EV company.")
    await store.append_message("s1", "user", "Continue the previous topic")

    history = await store.get_recent_history("s1", turns=2)

    assert len(history) == 3
    assert history[-1]["content"] == "Continue the previous topic"


@pytest.mark.asyncio
async def test_conversation_memory_search_returns_relevant_messages():
    store = ConversationMemoryStore()
    await store.append_message("s1", "user", "Let's discuss BYD and EVs")
    await store.append_message("s1", "assistant", "BYD has a large EV market share")
    await store.append_message("s1", "user", "Now talk about oil prices")

    matches = await store.search("s1", "BYD EV", limit=2)

    assert len(matches) >= 1
    assert "BYD" in matches[0]["content"]


@pytest.mark.asyncio
async def test_conversation_memory_context_window_keeps_recent_turn_and_relevant_older_turn():
    store = ConversationMemoryStore()
    await store.append_message("s1", "user", "Please remember the supplier battery issue")
    await store.append_message(
        "s1",
        "assistant",
        "Supplier Alpha is delayed because logistics is blocked.",
    )
    await store.append_message("s1", "user", "Thanks")
    await store.append_message("s1", "assistant", "Acknowledged")
    await store.append_message("s1", "user", "Let's switch to weekend planning")
    await store.append_message("s1", "assistant", "Weekend plan is still open.")

    history = await store.get_context_window(
        "s1",
        query="supplier logistics battery update",
        turns=2,
        min_recent_turns=1,
        max_tokens=80,
    )

    contents = [item["content"] for item in history]
    assert "Please remember the supplier battery issue" in contents
    assert "Supplier Alpha is delayed because logistics is blocked." in contents
    assert "Let's switch to weekend planning" in contents
    assert "Weekend plan is still open." in contents
    assert "Acknowledged" not in contents


@pytest.mark.asyncio
async def test_conversation_memory_context_window_respects_token_budget():
    store = ConversationMemoryStore()
    await store.append_message(
        "s2",
        "user",
        "supplier logistics battery schedule contract escalation timeline details",
    )
    await store.append_message(
        "s2",
        "assistant",
        "supplier logistics battery schedule contract escalation timeline details",
    )
    await store.append_message("s2", "user", "Recent ping")
    await store.append_message("s2", "assistant", "Recent pong")

    history = await store.get_context_window(
        "s2",
        query="supplier logistics battery",
        turns=2,
        min_recent_turns=1,
        max_tokens=4,
    )

    contents = [item["content"] for item in history]
    assert contents == ["Recent ping", "Recent pong"]


@pytest.mark.asyncio
async def test_conversation_memory_sqlite_persists_messages(tmp_path: Path):
    db_path = tmp_path / "memory.sqlite3"
    store = ConversationMemoryStore(backend="sqlite", sqlite_path=str(db_path))
    await store.append_message(
        "session-a",
        "user",
        "Persistent message",
        user_id="user-1",
        metadata={"tag": "persisted"},
    )
    reloaded = ConversationMemoryStore(backend="sqlite", sqlite_path=str(db_path))

    history = await reloaded.get_recent_history("session-a", turns=2)
    matches = await reloaded.search("session-a", "Persistent", limit=1)

    assert history[0]["content"] == "Persistent message"
    assert matches[0]["metadata"]["tag"] == "persisted"


@pytest.mark.asyncio
async def test_conversation_memory_mongo_persists_messages():
    mongo_collection = _FakeMongoCollection()
    store = ConversationMemoryStore(
        backend="mongo",
        mongo_uri="mongodb://unit-test",
        mongo_collection_handle=mongo_collection,
    )
    await store.append_message(
        "session-a",
        "user",
        "Persistent Mongo message",
        user_id="user-1",
        metadata={"tag": "persisted"},
    )
    reloaded = ConversationMemoryStore(
        backend="mongo",
        mongo_uri="mongodb://unit-test",
        mongo_collection_handle=mongo_collection,
    )

    history = await reloaded.get_recent_history("session-a", turns=2)
    matches = await reloaded.search("session-a", "Persistent", limit=1)

    assert history[0]["content"] == "Persistent Mongo message"
    assert matches[0]["metadata"]["tag"] == "persisted"


@pytest.mark.asyncio
async def test_cross_session_store_searches_prior_sessions(tmp_path: Path):
    db_path = tmp_path / "memory.sqlite3"
    memory_store = ConversationMemoryStore(backend="sqlite", sqlite_path=str(db_path))
    await memory_store.append_message(
        "session-old",
        "user",
        "Recall the supplier issue",
        user_id="user-42",
    )
    await memory_store.append_message(
        "session-old",
        "assistant",
        "The supplier delay was caused by logistics",
        user_id="user-42",
    )
    await memory_store.append_message(
        "session-new",
        "user",
        "Unrelated note",
        user_id="user-42",
    )
    cross_session_store = CrossSessionStore(conversation_memory=memory_store)

    matches = await cross_session_store.search(
        "user-42",
        "supplier logistics",
        limit=5,
        exclude_session_id="session-new",
    )

    assert len(matches) >= 1
    assert "supplier" in matches[0]["content"].lower()
    assert matches[0]["session_id"] == "session-old"


@pytest.mark.asyncio
async def test_cross_session_store_vector_backend_uses_mongo_and_qdrant():
    mongo_collection = _FakeMongoCollection()
    qdrant_client = _FakeQdrantClient()
    store = CrossSessionStore(
        backend="mongo_qdrant",
        embedding_func=_FakeEmbeddingFunc(),
        mongo_uri="mongodb://unit-test",
        mongo_collection_handle=mongo_collection,
        qdrant_url="http://qdrant.test",
        qdrant_client=qdrant_client,
        min_content_chars=10,
        max_content_chars=120,
    )

    await store.index_message(
        MemoryMessage(
            session_id="session-old",
            role="assistant",
            content="The supplier issue was caused by logistics delays.",
            timestamp="2026-04-03T00:00:00+00:00",
            user_id="user-1",
            metadata={"workspace": "ops"},
        )
    )
    await store.index_message(
        MemoryMessage(
            session_id="session-new",
            role="assistant",
            content="Battery output improved this quarter.",
            timestamp="2026-04-03T01:00:00+00:00",
            user_id="user-1",
            metadata={"workspace": "ops"},
        )
    )
    await store.index_message(
        MemoryMessage(
            session_id="session-other-user",
            role="assistant",
            content="Supplier note for another user.",
            timestamp="2026-04-03T02:00:00+00:00",
            user_id="user-2",
            metadata={"workspace": "ops"},
        )
    )

    matches = await store.search(
        "user-1",
        "supplier logistics",
        limit=5,
        exclude_session_id="session-new",
    )

    assert len(matches) == 1
    assert matches[0]["session_id"] == "session-old"
    assert matches[0]["user_id"] == "user-1"
    assert matches[0]["score"] > 0
    assert "supplier" in matches[0]["content"].lower()


@pytest.mark.asyncio
async def test_cross_session_store_deduplicates_duplicate_messages_across_sessions():
    mongo_collection = _FakeMongoCollection()
    qdrant_client = _FakeQdrantClient()
    store = CrossSessionStore(
        backend="mongo_qdrant",
        embedding_func=_FakeEmbeddingFunc(),
        mongo_uri="mongodb://unit-test",
        mongo_collection_handle=mongo_collection,
        qdrant_url="http://qdrant.test",
        qdrant_client=qdrant_client,
        min_content_chars=10,
        max_content_chars=160,
        max_session_refs=3,
    )

    repeated_text = "Supplier updates were delayed because logistics slowed down."
    await store.index_message(
        MemoryMessage(
            session_id="session-a",
            role="assistant",
            content=repeated_text,
            timestamp="2026-04-03T00:00:00+00:00",
            user_id="user-1",
            metadata={"workspace": "ops"},
        )
    )
    await store.index_message(
        MemoryMessage(
            session_id="session-b",
            role="assistant",
            content="  Supplier updates were delayed because logistics slowed down.  ",
            timestamp="2026-04-03T01:00:00+00:00",
            user_id="user-1",
            metadata={"workspace": "ops"},
        )
    )

    assert len(mongo_collection.documents) == 1
    stored_document = next(iter(mongo_collection.documents.values()))
    assert stored_document["occurrence_count"] == 2
    assert stored_document["session_ids"] == ["session-a", "session-b"]
    assert stored_document["last_seen_at"] == "2026-04-03T01:00:00+00:00"
    qdrant_bucket = next(iter(qdrant_client.collections.values()))
    assert len(qdrant_bucket) == 1

    matches = await store.search("user-1", "supplier logistics", limit=5)
    assert len(matches) == 1
    assert matches[0]["occurrence_count"] == 2
    assert matches[0]["session_ids"] == ["session-a", "session-b"]


@pytest.mark.asyncio
async def test_cross_session_store_consolidates_similar_messages_into_cluster_summary():
    mongo_collection = _FakeMongoCollection()
    qdrant_client = _FakeQdrantClient()
    store = CrossSessionStore(
        backend="mongo_qdrant",
        embedding_func=_FakeEmbeddingFunc(),
        mongo_uri="mongodb://unit-test",
        mongo_collection_handle=mongo_collection,
        qdrant_url="http://qdrant.test",
        qdrant_client=qdrant_client,
        min_content_chars=10,
        max_content_chars=160,
        consolidation_similarity_threshold=0.8,
        consolidation_top_k=2,
        max_cluster_snippets=4,
    )

    await store.index_message(
        MemoryMessage(
            session_id="session-a",
            role="assistant",
            content="Supplier shipments slipped because logistics kept stalling.",
            timestamp="2026-04-03T00:00:00+00:00",
            user_id="user-1",
            metadata={"workspace": "ops"},
        )
    )
    await store.index_message(
        MemoryMessage(
            session_id="session-b",
            role="assistant",
            content="Logistics delays are still affecting supplier deliveries this week.",
            timestamp="2026-04-03T01:00:00+00:00",
            user_id="user-1",
            metadata={"workspace": "ops"},
        )
    )

    assert len(mongo_collection.documents) == 1
    stored_document = next(iter(mongo_collection.documents.values()))
    assert stored_document["occurrence_count"] == 2
    assert stored_document["cluster_size"] == 2
    assert stored_document["source_snippet_count"] == 2
    assert stored_document["consolidation_strategy"] == "semantic_cluster"
    assert stored_document["summary_strategy"] in {"cluster_summary", "cluster_head_tail"}
    assert "supplier" in stored_document["content"].lower()
    assert "logistics" in stored_document["content"].lower()

    matches = await store.search("user-1", "supplier logistics", limit=5)
    assert len(matches) == 1
    assert matches[0]["cluster_size"] == 2
    assert matches[0]["source_snippet_count"] == 2
    assert matches[0]["consolidation_strategy"] == "semantic_cluster"


@pytest.mark.asyncio
async def test_cross_session_store_compresses_long_messages_and_skips_low_signal():
    mongo_collection = _FakeMongoCollection()
    qdrant_client = _FakeQdrantClient()
    store = CrossSessionStore(
        backend="mongo_qdrant",
        embedding_func=_FakeEmbeddingFunc(),
        mongo_uri="mongodb://unit-test",
        mongo_collection_handle=mongo_collection,
        qdrant_url="http://qdrant.test",
        qdrant_client=qdrant_client,
        min_content_chars=10,
        max_content_chars=80,
    )

    await store.index_message(
        MemoryMessage(
            session_id="session-noise",
            role="user",
            content="Thanks",
            timestamp="2026-04-03T00:00:00+00:00",
            user_id="user-1",
            metadata={},
        )
    )
    assert mongo_collection.documents == {}

    long_text = (
        "Supplier update: logistics delays continue. "
        "Supplier update: logistics delays continue. "
        "Battery planning stays unchanged. "
        "Battery planning stays unchanged. "
        "Supplier update: logistics delays continue."
    )
    await store.index_message(
        MemoryMessage(
            session_id="session-long",
            role="assistant",
            content=long_text,
            timestamp="2026-04-03T01:00:00+00:00",
            user_id="user-1",
            metadata={"workspace": "ops"},
        )
    )

    assert len(mongo_collection.documents) == 1
    stored_document = next(iter(mongo_collection.documents.values()))
    assert stored_document["indexed_char_count"] <= 80
    assert stored_document["compression_applied"] is True
    assert stored_document["compression_strategy"] in {
        "dedup_normalize",
        "sentence_pack",
        "head_tail",
    }


@pytest.mark.asyncio
async def test_cross_session_store_background_reconsolidation_merges_existing_docs():
    mongo_collection = _FakeMongoCollection()
    qdrant_client = _FakeQdrantClient()
    store = CrossSessionStore(
        backend="mongo_qdrant",
        embedding_func=_FakeEmbeddingFunc(),
        mongo_uri="mongodb://unit-test",
        mongo_collection_handle=mongo_collection,
        qdrant_url="http://qdrant.test",
        qdrant_client=qdrant_client,
        min_content_chars=10,
        max_content_chars=160,
        enable_consolidation=False,
        consolidation_similarity_threshold=0.8,
        consolidation_top_k=3,
        enable_background_maintenance=True,
        maintenance_batch_size=10,
    )

    await store.index_message(
        MemoryMessage(
            session_id="session-a",
            role="assistant",
            content="Supplier shipments slipped because logistics kept stalling.",
            timestamp="2026-03-01T00:00:00+00:00",
            user_id="user-1",
            metadata={"workspace": "ops"},
        )
    )
    await store.index_message(
        MemoryMessage(
            session_id="session-b",
            role="assistant",
            content="Logistics delays are still affecting supplier deliveries this week.",
            timestamp="2026-03-02T00:00:00+00:00",
            user_id="user-1",
            metadata={"workspace": "ops"},
        )
    )

    assert len(mongo_collection.documents) == 2

    store.enable_consolidation = True
    stats = await store.run_maintenance_once()

    assert stats["merged_docs"] == 1
    assert len(mongo_collection.documents) == 1
    stored_document = next(iter(mongo_collection.documents.values()))
    assert stored_document["cluster_size"] == 2
    assert stored_document["occurrence_count"] == 2
    assert stored_document["consolidation_strategy"] == "background_reconsolidation"
    qdrant_bucket = next(iter(qdrant_client.collections.values()))
    assert len(qdrant_bucket) == 1


@pytest.mark.asyncio
async def test_cross_session_store_aging_deletes_stale_low_support_docs():
    mongo_collection = _FakeMongoCollection()
    qdrant_client = _FakeQdrantClient()
    store = CrossSessionStore(
        backend="mongo_qdrant",
        embedding_func=_FakeEmbeddingFunc(),
        mongo_uri="mongodb://unit-test",
        mongo_collection_handle=mongo_collection,
        qdrant_url="http://qdrant.test",
        qdrant_client=qdrant_client,
        min_content_chars=10,
        max_content_chars=160,
        enable_background_maintenance=True,
        maintenance_batch_size=10,
        aging_stale_after_days=0.5,
        aging_delete_after_days=1.0,
        aging_keep_min_occurrences=2,
    )

    await store.index_message(
        MemoryMessage(
            session_id="session-old",
            role="assistant",
            content="Supplier planning note with logistics detail.",
            timestamp="2026-01-01T00:00:00+00:00",
            user_id="user-1",
            metadata={"workspace": "ops"},
        )
    )

    stats = await store.run_maintenance_once()

    assert stats["deleted_docs"] == 1
    assert mongo_collection.documents == {}
    qdrant_bucket = next(iter(qdrant_client.collections.values()))
    assert qdrant_bucket == {}


@pytest.mark.asyncio
async def test_cross_session_store_background_maintenance_loop_runs_and_stops():
    mongo_collection = _FakeMongoCollection()
    qdrant_client = _FakeQdrantClient()
    store = CrossSessionStore(
        backend="mongo_qdrant",
        embedding_func=_FakeEmbeddingFunc(),
        mongo_uri="mongodb://unit-test",
        mongo_collection_handle=mongo_collection,
        qdrant_url="http://qdrant.test",
        qdrant_client=qdrant_client,
        min_content_chars=10,
        max_content_chars=160,
        enable_background_maintenance=True,
        maintenance_interval_seconds=1,
    )

    await store.index_message(
        MemoryMessage(
            session_id="session-a",
            role="assistant",
            content="Supplier baseline note for maintenance loop.",
            timestamp="2026-04-03T00:00:00+00:00",
            user_id="user-1",
            metadata={"workspace": "ops"},
        )
    )

    calls = 0
    original_run = store.run_maintenance_once

    async def wrapped_run():
        nonlocal calls
        calls += 1
        return await original_run()

    store.run_maintenance_once = wrapped_run

    started = await store.start_background_maintenance()
    assert started is True
    assert store.maintenance_running is True

    for _ in range(20):
        if calls > 0:
            break
        await asyncio.sleep(0.01)

    await store.stop_background_maintenance()

    assert calls >= 1
    assert store.maintenance_running is False


@pytest.mark.asyncio
async def test_user_profile_store_sqlite_persists_attributes(tmp_path: Path):
    db_path = tmp_path / "profiles.sqlite3"
    store = UserProfileStore(backend="sqlite", sqlite_path=str(db_path))
    await store.update_profile("user-7", {"industry": "energy", "locale": "zh-CN"})

    reloaded = UserProfileStore(backend="sqlite", sqlite_path=str(db_path))
    profile = await reloaded.get_profile("user-7")

    assert profile == {"industry": "energy", "locale": "zh-CN"}


@pytest.mark.asyncio
async def test_user_profile_store_mongo_persists_attributes():
    mongo_collection = _FakeMongoCollection()
    store = UserProfileStore(
        backend="mongo",
        mongo_uri="mongodb://unit-test",
        mongo_collection_handle=mongo_collection,
    )
    await store.update_profile("user-7", {"industry": "energy", "locale": "zh-CN"})
    await store.update_profile("user-7", {"focus": "battery"})

    reloaded = UserProfileStore(
        backend="mongo",
        mongo_uri="mongodb://unit-test",
        mongo_collection_handle=mongo_collection,
    )
    profile = await reloaded.get_profile("user-7")

    assert profile == {
        "industry": "energy",
        "locale": "zh-CN",
        "focus": "battery",
    }
