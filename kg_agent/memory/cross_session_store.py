from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from typing import Any

from kg_agent.memory.conversation_memory import ConversationMemoryStore, MemoryMessage


_IDENTIFIER_PATTERN = re.compile(r"[^a-z0-9_]+")
_WHITESPACE_PATTERN = re.compile(r"\s+")
_SENTENCE_SPLIT_PATTERN = re.compile(
    r"(?<=[.!?\u3002\uff01\uff1f\uff1b;])\s+|\n+"
)
_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]")
_LOW_SIGNAL_PATTERN = re.compile(
    r"^(?:ok|okay|thanks|thank you|got it|continue|go on|noted|understood|"
    r"\u597d\u7684|\u597d|\u8c22\u8c22|\u6536\u5230|\u7ee7\u7eed|\u6069|\u54e6)"
    r"[.!?\u3002\uff01\uff1f]*$",
    re.IGNORECASE,
)


class _FallbackQdrantDistance:
    COSINE = "cosine"


class _FallbackQdrantVectorParams:
    def __init__(self, *, size: int, distance: str):
        self.size = size
        self.distance = distance


class _FallbackQdrantKeywordIndexType:
    KEYWORD = "keyword"


class _FallbackQdrantKeywordIndexParams:
    def __init__(self, *, type: str):
        self.type = type


class _FallbackQdrantMatchValue:
    def __init__(self, *, value: Any):
        self.value = value


class _FallbackQdrantFieldCondition:
    def __init__(self, *, key: str, match: Any):
        self.key = key
        self.match = match


class _FallbackQdrantFilter:
    def __init__(self, *, must: list[Any] | None = None, must_not: list[Any] | None = None):
        self.must = list(must or [])
        self.must_not = list(must_not or [])


class _FallbackQdrantPointStruct:
    def __init__(self, *, id: str, vector: list[float], payload: dict[str, Any]):
        self.id = id
        self.vector = vector
        self.payload = payload


class _FallbackQdrantPointIdsList:
    def __init__(self, *, points: list[str]):
        self.points = list(points)


class _FallbackQdrantModels:
    Distance = _FallbackQdrantDistance
    VectorParams = _FallbackQdrantVectorParams
    KeywordIndexType = _FallbackQdrantKeywordIndexType
    KeywordIndexParams = _FallbackQdrantKeywordIndexParams
    MatchValue = _FallbackQdrantMatchValue
    FieldCondition = _FallbackQdrantFieldCondition
    Filter = _FallbackQdrantFilter
    PointStruct = _FallbackQdrantPointStruct
    PointIdsList = _FallbackQdrantPointIdsList


def _normalize_backend(value: str | None) -> str:
    normalized = (value or "memory").strip().lower()
    if normalized in {"mongo_qdrant", "mongo-qdrant", "mongo+qdrant", "vector"}:
        return "mongo_qdrant"
    if normalized == "auto":
        return "auto"
    return "memory"


def _json_safe(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return str(value)


def _normalize_identifier(value: str | None, *, fallback: str) -> str:
    sanitized = _IDENTIFIER_PATTERN.sub("_", (value or "").strip().lower()).strip("_")
    return sanitized or fallback


def _normalize_text(value: str) -> str:
    return _WHITESPACE_PATTERN.sub(" ", (value or "").strip())


def _dedup_strings(values: list[str], *, limit: int) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        normalized = (value or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    if limit > 0 and len(ordered) > limit:
        return ordered[-limit:]
    return ordered


def _tokenize(text: str) -> list[str]:
    return [match.group(0).casefold() for match in _TOKEN_PATTERN.finditer(text or "")]


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class CrossSessionStore:
    def __init__(
        self,
        *,
        conversation_memory: ConversationMemoryStore | None = None,
        backend: str = "memory",
        embedding_func: Any | None = None,
        mongo_uri: str | None = None,
        mongo_database: str | None = None,
        mongo_collection: str = "kg_agent_cross_session_messages",
        mongo_collection_handle: Any | None = None,
        qdrant_url: str | None = None,
        qdrant_api_key: str | None = None,
        qdrant_collection_prefix: str = "kg_agent_cross_session",
        qdrant_client: Any | None = None,
        min_content_chars: int = 8,
        max_content_chars: int = 1200,
        max_session_refs: int = 12,
        enable_consolidation: bool = True,
        consolidation_similarity_threshold: float = 0.82,
        consolidation_top_k: int = 3,
        max_cluster_snippets: int = 6,
        enable_background_maintenance: bool = False,
        maintenance_interval_seconds: int = 1800,
        maintenance_batch_size: int = 100,
        aging_stale_after_days: float = 14.0,
        aging_delete_after_days: float = 60.0,
        aging_keep_min_occurrences: int = 2,
        aging_max_snippets: int = 3,
    ):
        self.conversation_memory = conversation_memory
        self.backend = _normalize_backend(backend)
        self.embedding_func = embedding_func

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
            (mongo_collection or "").strip() or "kg_agent_cross_session_messages"
        )
        self.qdrant_url = (
            (qdrant_url or "").strip() or os.getenv("QDRANT_URL", "").strip()
        )
        self.qdrant_api_key = (
            (qdrant_api_key or "").strip() or os.getenv("QDRANT_API_KEY", "").strip()
        )
        self.qdrant_collection_prefix = (
            (qdrant_collection_prefix or "").strip() or "kg_agent_cross_session"
        )
        self.min_content_chars = max(1, int(min_content_chars or 8))
        self.max_content_chars = max(self.min_content_chars, int(max_content_chars or 1200))
        self.max_session_refs = max(1, int(max_session_refs or 12))
        self.enable_consolidation = bool(enable_consolidation)
        self.consolidation_similarity_threshold = float(
            consolidation_similarity_threshold or 0.82
        )
        self.consolidation_top_k = max(1, int(consolidation_top_k or 3))
        self.max_cluster_snippets = max(1, int(max_cluster_snippets or 6))
        self.enable_background_maintenance = bool(enable_background_maintenance)
        self.maintenance_interval_seconds = max(
            1, int(maintenance_interval_seconds or 1800)
        )
        self.maintenance_batch_size = max(1, int(maintenance_batch_size or 100))
        self.aging_stale_after_days = max(
            0.0, float(aging_stale_after_days if aging_stale_after_days is not None else 14.0)
        )
        self.aging_delete_after_days = max(
            self.aging_stale_after_days,
            float(aging_delete_after_days if aging_delete_after_days is not None else 60.0),
        )
        self.aging_keep_min_occurrences = max(
            1, int(aging_keep_min_occurrences or 2)
        )
        self.aging_max_snippets = max(1, int(aging_max_snippets or 3))

        self._mongo_collection = mongo_collection_handle
        self._mongo_client = None
        self._qdrant_client = qdrant_client
        self._qdrant_models = None
        self._qdrant_collection_name = self._build_qdrant_collection_name()
        self._lock = asyncio.Lock()
        self._vector_backend_ready = False
        self._vector_init_attempted = False
        self._disabled_reason: str | None = None
        self._maintenance_task: asyncio.Task | None = None
        self._maintenance_last_stats: dict[str, Any] = {}
        self._maintenance_last_run_at: str | None = None

    @property
    def disabled_reason(self) -> str | None:
        return self._disabled_reason

    @property
    def maintenance_running(self) -> bool:
        return self._maintenance_task is not None and not self._maintenance_task.done()

    async def index_message(self, message: MemoryMessage) -> None:
        if not message.user_id or not message.session_id:
            return
        if not isinstance(message.content, str) or not message.content.strip():
            return
        if not await self._ensure_vector_backend_ready():
            return

        try:
            incoming = self._build_message_document(message)
            if incoming is None:
                return

            current = await self._find_existing_document(incoming["_id"])
            strategy = "singleton"
            if current is None and self.enable_consolidation:
                cluster_candidate = await self._find_cluster_candidate(incoming)
                if cluster_candidate is not None:
                    current = cluster_candidate
                    strategy = "semantic_cluster"
            elif current is not None:
                strategy = "exact_duplicate"

            merged = self._merge_documents(current, incoming, strategy=strategy)
            await self._upsert_document(merged)
            await self._upsert_vector(merged)
        except Exception as exc:
            self._disabled_reason = str(exc)
            self._vector_backend_ready = False

    async def search(
        self,
        user_id: str | None,
        query: str,
        limit: int = 5,
        *,
        exclude_session_id: str | None = None,
    ) -> list[dict[str, Any]]:
        normalized_user_id = (user_id or "").strip()
        if not normalized_user_id:
            return []

        if await self._ensure_vector_backend_ready():
            try:
                matches = await self._search_vector_backend(
                    normalized_user_id,
                    query=query,
                    limit=limit,
                    exclude_session_id=exclude_session_id,
                )
            except Exception as exc:
                self._disabled_reason = str(exc)
                self._vector_backend_ready = False
                matches = []
            if matches:
                return matches

        if self.conversation_memory is None:
            return []
        return await self.conversation_memory.search_user_sessions(
            normalized_user_id,
            query=query,
            limit=limit,
            exclude_session_id=exclude_session_id,
        )

    async def start_background_maintenance(self) -> bool:
        if self.maintenance_running:
            return True
        if not self.enable_background_maintenance:
            return False
        if not await self._ensure_vector_backend_ready():
            return False
        self._maintenance_task = asyncio.create_task(
            self._maintenance_loop(),
            name="kg_agent_cross_session_maintenance",
        )
        return True

    async def stop_background_maintenance(self) -> None:
        task = self._maintenance_task
        self._maintenance_task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def run_maintenance_once(self) -> dict[str, Any]:
        if not await self._ensure_vector_backend_ready():
            return {
                "processed_docs": 0,
                "merged_docs": 0,
                "aged_docs": 0,
                "deleted_docs": 0,
                "backend_ready": False,
            }

        stats = {
            "processed_docs": 0,
            "merged_docs": 0,
            "aged_docs": 0,
            "deleted_docs": 0,
            "backend_ready": True,
        }
        documents = await self._list_documents(limit=self.maintenance_batch_size)
        processed_ids: set[str] = set()
        for document in documents:
            document_id = str(document.get("_id") or "")
            if not document_id or document_id in processed_ids:
                continue
            latest = await self._find_existing_document(document_id)
            if latest is None:
                continue
            processed_ids.add(document_id)
            stats["processed_docs"] += 1

            if self.enable_consolidation:
                merge_result = await self._maybe_reconsolidate_document(latest)
                if merge_result["merged"]:
                    stats["merged_docs"] += 1
                    if merge_result["deleted_id"]:
                        stats["deleted_docs"] += 1
                        processed_ids.add(merge_result["deleted_id"])
                    latest = merge_result["document"] or latest

            aging_result = await self._apply_aging_policy(latest)
            if aging_result["deleted"]:
                stats["deleted_docs"] += 1
                continue
            if aging_result["updated"]:
                stats["aged_docs"] += 1

        self._maintenance_last_run_at = datetime.now(timezone.utc).isoformat()
        self._maintenance_last_stats = {
            **stats,
            "run_at": self._maintenance_last_run_at,
        }
        return dict(self._maintenance_last_stats)

    async def close(self) -> None:
        await self.stop_background_maintenance()
        if self._mongo_client is not None:
            close_func = getattr(self._mongo_client, "close", None)
            if callable(close_func):
                result = close_func()
                if asyncio.iscoroutine(result):
                    await result
            self._mongo_client = None
        if self._qdrant_client is not None:
            close_func = getattr(self._qdrant_client, "close", None)
            if callable(close_func):
                await asyncio.to_thread(close_func)
            self._qdrant_client = None

    async def _ensure_vector_backend_ready(self) -> bool:
        if self._vector_backend_ready:
            return True

        async with self._lock:
            if self._vector_backend_ready:
                return True
            if self._vector_init_attempted:
                return False

            self._vector_init_attempted = True
            if not self._vector_backend_requested():
                return False

            try:
                await self._initialize_mongo_collection()
                await self._initialize_qdrant_collection()
            except Exception as exc:
                self._disabled_reason = str(exc)
                self._vector_backend_ready = False
                return False

            self._vector_backend_ready = True
            return True

    def _vector_backend_requested(self) -> bool:
        if self.backend == "memory":
            self._disabled_reason = "Cross-session vector backend is disabled"
            return False
        if self.embedding_func is None:
            self._disabled_reason = "Embedding function is not configured"
            return False
        if self.backend == "auto" and not (self.mongo_uri and self.qdrant_url):
            self._disabled_reason = (
                "Cross-session auto backend requires both Mongo and Qdrant URLs"
            )
            return False
        if not self.mongo_uri:
            self._disabled_reason = "Mongo URI is not configured"
            return False
        if not self.qdrant_url:
            self._disabled_reason = "Qdrant URL is not configured"
            return False
        return True

    def _build_qdrant_collection_name(self) -> str:
        prefix = _normalize_identifier(
            self.qdrant_collection_prefix, fallback="kg_agent_cross_session"
        )
        embedding_dim = getattr(self.embedding_func, "embedding_dim", None)
        model_name = _normalize_identifier(
            getattr(self.embedding_func, "model_name", None),
            fallback="embedding",
        )
        if embedding_dim:
            return f"{prefix}_{model_name}_{embedding_dim}d"[:160].rstrip("_")
        return f"{prefix}_{model_name}"[:160].rstrip("_")

    def _build_message_document(self, message: MemoryMessage) -> dict[str, Any] | None:
        normalized_content = _normalize_text(message.content)
        if self._should_skip_message(normalized_content):
            return None

        compressed_content, compression_stats = self._compress_content(normalized_content)
        metadata = message.metadata if isinstance(message.metadata, dict) else {}
        safe_metadata = _json_safe(metadata)
        content_fingerprint = self._build_content_fingerprint(
            role=message.role,
            content=normalized_content,
        )
        snippet = self._build_source_snippet(
            content=compressed_content,
            timestamp=message.timestamp,
            session_id=message.session_id,
            fingerprint=content_fingerprint,
        )
        document_id = hashlib.sha256(
            f"{message.user_id}|{content_fingerprint}".encode("utf-8")
        ).hexdigest()
        document = {
            "_id": document_id,
            "user_id": message.user_id,
            "role": message.role,
            "content": compressed_content,
            "timestamp": message.timestamp,
            "first_seen_at": message.timestamp,
            "last_seen_at": message.timestamp,
            "occurrence_count": 1,
            "session_ids": [message.session_id],
            "content_fingerprint": content_fingerprint,
            "member_fingerprints": [content_fingerprint],
            "source_snippets": [snippet],
            "source_snippet_count": 1,
            "cluster_size": 1,
            "consolidation_strategy": "singleton",
            "summary_strategy": "single_snippet",
            "metadata": safe_metadata if isinstance(safe_metadata, dict) else {},
            **compression_stats,
        }
        workspace = document["metadata"].get("workspace")
        if isinstance(workspace, str) and workspace.strip():
            document["workspace"] = workspace.strip()
        return document

    def _build_source_snippet(
        self,
        *,
        content: str,
        timestamp: str,
        session_id: str,
        fingerprint: str,
    ) -> dict[str, str]:
        return {
            "content": content,
            "timestamp": timestamp,
            "session_id": session_id,
            "fingerprint": fingerprint,
        }

    def _should_skip_message(self, content: str) -> bool:
        if len(content) < self.min_content_chars:
            return True
        if _LOW_SIGNAL_PATTERN.match(content):
            return True
        tokens = _tokenize(content)
        if len(tokens) <= 1 and len(content) < max(self.min_content_chars * 2, 24):
            return True
        return False

    def _compress_content(self, content: str) -> tuple[str, dict[str, Any]]:
        original_length = len(content)
        segments = self._deduplicate_segments(content)
        candidate = " ".join(segments).strip() if segments else content
        candidate = _normalize_text(candidate)

        if len(candidate) <= self.max_content_chars:
            strategy = "dedup_normalize" if len(candidate) != original_length else "normalize"
            return candidate, {
                "raw_char_count": original_length,
                "indexed_char_count": len(candidate),
                "compression_applied": len(candidate) != original_length,
                "compression_strategy": strategy,
            }

        packed = self._pack_sentences(candidate)
        if packed != candidate and len(packed) <= self.max_content_chars:
            return packed, {
                "raw_char_count": original_length,
                "indexed_char_count": len(packed),
                "compression_applied": True,
                "compression_strategy": "sentence_pack",
            }

        truncated = self._head_tail_truncate(candidate)
        return truncated, {
            "raw_char_count": original_length,
            "indexed_char_count": len(truncated),
            "compression_applied": True,
            "compression_strategy": "head_tail",
        }

    def _deduplicate_segments(self, content: str) -> list[str]:
        seen: set[str] = set()
        ordered: list[str] = []
        for segment in _SENTENCE_SPLIT_PATTERN.split(content):
            normalized = _normalize_text(segment)
            if not normalized:
                continue
            signature = normalized.casefold()
            if signature in seen:
                continue
            seen.add(signature)
            ordered.append(normalized)
        return ordered

    def _pack_sentences(self, content: str) -> str:
        packed: list[str] = []
        current_length = 0
        for segment in self._deduplicate_segments(content):
            extra = len(segment) if not packed else len(segment) + 1
            if packed and current_length + extra > self.max_content_chars:
                break
            if not packed and len(segment) > self.max_content_chars:
                return self._head_tail_truncate(segment)
            packed.append(segment)
            current_length += extra
        packed_content = " ".join(packed).strip()
        return packed_content or self._head_tail_truncate(content)

    def _head_tail_truncate(self, content: str) -> str:
        if len(content) <= self.max_content_chars:
            return content
        if self.max_content_chars <= 20:
            return content[: self.max_content_chars]

        marker = " ... "
        remaining = self.max_content_chars - len(marker)
        head_chars = max(1, int(remaining * 0.7))
        tail_chars = max(1, remaining - head_chars)
        head = content[:head_chars].rstrip()
        tail = content[-tail_chars:].lstrip()
        return f"{head}{marker}{tail}"

    def _build_content_fingerprint(self, *, role: str, content: str) -> str:
        tokens = _tokenize(content)
        fingerprint_basis = " ".join(tokens) if tokens else content.casefold()
        return hashlib.sha256(f"{role}|{fingerprint_basis}".encode("utf-8")).hexdigest()

    async def _maintenance_loop(self) -> None:
        while True:
            try:
                await self.run_maintenance_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._disabled_reason = str(exc)
            await asyncio.sleep(self.maintenance_interval_seconds)

    async def _list_documents(self, *, limit: int) -> list[dict[str, Any]]:
        if self._mongo_collection is None:
            return []
        cursor = self._mongo_collection.find({})
        rows = await cursor.to_list(length=limit)
        documents = [row for row in rows if isinstance(row, dict)]
        documents.sort(key=lambda item: str(item.get("last_seen_at") or ""))
        return documents[:limit]

    async def _initialize_mongo_collection(self) -> None:
        if self._mongo_collection is not None:
            return

        try:
            from pymongo import AsyncMongoClient
        except ImportError as exc:
            raise RuntimeError("pymongo is required for cross-session Mongo storage") from exc

        self._mongo_client = AsyncMongoClient(self.mongo_uri)
        database = self._mongo_client.get_database(self.mongo_database)
        self._mongo_collection = database.get_collection(self.mongo_collection_name)
        await self._mongo_collection.create_index([("user_id", 1), ("last_seen_at", -1)])
        await self._mongo_collection.create_index([("user_id", 1), ("session_ids", 1)])

    async def _initialize_qdrant_collection(self) -> None:
        if self._qdrant_client is None:
            try:
                from qdrant_client import QdrantClient, models
            except ImportError as exc:
                raise RuntimeError(
                    "qdrant-client is required for cross-session vector storage"
                ) from exc
            self._qdrant_models = models
            self._qdrant_client = QdrantClient(
                url=self.qdrant_url,
                api_key=self.qdrant_api_key or None,
            )
        elif self._qdrant_models is None:
            try:
                from qdrant_client import models
            except ImportError:
                # Tests may inject a fake client without the real qdrant SDK installed.
                self._qdrant_models = _FallbackQdrantModels
            else:
                self._qdrant_models = models

        models = self._qdrant_models
        collection_exists = await asyncio.to_thread(
            self._qdrant_client.collection_exists,
            self._qdrant_collection_name,
        )
        if not collection_exists:
            await asyncio.to_thread(
                self._qdrant_client.create_collection,
                collection_name=self._qdrant_collection_name,
                vectors_config=models.VectorParams(
                    size=getattr(self.embedding_func, "embedding_dim", 1536) or 1536,
                    distance=models.Distance.COSINE,
                ),
            )

        for field_name in ("user_id", "session_ids", "role"):
            await asyncio.to_thread(
                self._qdrant_client.create_payload_index,
                collection_name=self._qdrant_collection_name,
                field_name=field_name,
                field_schema=models.KeywordIndexParams(
                    type=models.KeywordIndexType.KEYWORD,
                ),
            )

    async def _find_existing_document(self, document_id: str) -> dict[str, Any] | None:
        if self._mongo_collection is None:
            return None
        find_one = getattr(self._mongo_collection, "find_one", None)
        if callable(find_one):
            return await find_one({"_id": document_id})

        cursor = self._mongo_collection.find({"_id": {"$in": [document_id]}})
        rows = await cursor.to_list(length=1)
        if not rows:
            return None
        row = rows[0]
        return row if isinstance(row, dict) else None

    async def _find_cluster_candidate(
        self,
        incoming: dict[str, Any],
    ) -> dict[str, Any] | None:
        if self._qdrant_client is None or self._qdrant_models is None:
            return None

        matches = await self._query_points(
            query=incoming["content"],
            user_id=incoming["user_id"],
            role=incoming["role"],
            limit=self.consolidation_top_k,
        )
        if not matches:
            return None

        candidate_ids: list[str] = []
        score_by_id: dict[str, float] = {}
        for point in matches:
            payload = getattr(point, "payload", {}) or {}
            message_id = payload.get("message_id") or str(getattr(point, "id", ""))
            if not message_id or message_id == incoming["_id"]:
                continue
            score = float(getattr(point, "score", 0.0) or 0.0)
            if score < self.consolidation_similarity_threshold:
                continue
            candidate_ids.append(str(message_id))
            score_by_id[str(message_id)] = score

        if not candidate_ids:
            return None

        documents = await self._load_documents(candidate_ids)
        for candidate_id in candidate_ids:
            candidate = documents.get(candidate_id)
            if candidate and self._should_consolidate(
                candidate, incoming, score_by_id[candidate_id]
            ):
                return candidate
        return None

    def _should_consolidate(
        self,
        candidate: dict[str, Any],
        incoming: dict[str, Any],
        score: float,
    ) -> bool:
        if candidate.get("user_id") != incoming.get("user_id"):
            return False
        if candidate.get("role") != incoming.get("role"):
            return False
        if score < self.consolidation_similarity_threshold:
            return False
        if candidate.get("content_fingerprint") == incoming.get("content_fingerprint"):
            return True

        candidate_tokens = set(_tokenize(str(candidate.get("content", ""))))
        incoming_tokens = set(_tokenize(str(incoming.get("content", ""))))
        if not candidate_tokens or not incoming_tokens:
            return False
        return len(candidate_tokens & incoming_tokens) >= 1

    def _merge_documents(
        self,
        existing: dict[str, Any] | None,
        incoming: dict[str, Any],
        *,
        strategy: str,
    ) -> dict[str, Any]:
        if not isinstance(existing, dict):
            merged = dict(incoming)
            merged["consolidation_strategy"] = "singleton"
            merged["summary_strategy"] = "single_snippet"
            return merged

        snippets = self._merge_source_snippets(
            list(existing.get("source_snippets") or []),
            list(incoming.get("source_snippets") or []),
        )
        member_fingerprints = _dedup_strings(
            [
                *(str(value) for value in list(existing.get("member_fingerprints") or [])),
                *(str(value) for value in list(incoming.get("member_fingerprints") or [])),
            ],
            limit=self.max_cluster_snippets * 2,
        )
        summary = self._build_cluster_summary(snippets)

        merged = dict(existing)
        merged["_id"] = existing["_id"]
        merged["content"] = summary["content"]
        merged["summary_strategy"] = summary["summary_strategy"]
        merged["timestamp"] = incoming.get("timestamp", existing.get("timestamp"))
        merged["last_seen_at"] = incoming.get("last_seen_at", existing.get("last_seen_at"))
        merged["first_seen_at"] = existing.get("first_seen_at", incoming.get("first_seen_at"))
        merged["occurrence_count"] = _safe_int(existing.get("occurrence_count"), 1) + 1
        merged["session_ids"] = _dedup_strings(
            [
                *(str(value) for value in list(existing.get("session_ids") or [])),
                *(str(value) for value in list(incoming.get("session_ids") or [])),
            ],
            limit=self.max_session_refs,
        )
        merged["member_fingerprints"] = member_fingerprints
        merged["source_snippets"] = snippets
        merged["source_snippet_count"] = len(snippets)
        merged["cluster_size"] = len(member_fingerprints)
        merged["metadata"] = {
            **(existing.get("metadata") if isinstance(existing.get("metadata"), dict) else {}),
            **(incoming.get("metadata") if isinstance(incoming.get("metadata"), dict) else {}),
        }
        merged["raw_char_count"] = max(
            _safe_int(existing.get("raw_char_count")),
            _safe_int(incoming.get("raw_char_count")),
        )
        merged["indexed_char_count"] = len(summary["content"])
        merged["compression_applied"] = bool(
            existing.get("compression_applied") or incoming.get("compression_applied")
        ) or summary["summary_strategy"] != "single_snippet"
        merged["compression_strategy"] = (
            "cluster_summary"
            if summary["summary_strategy"] != "single_snippet"
            else incoming.get(
                "compression_strategy",
                existing.get("compression_strategy", "normalize"),
            )
        )
        merged["consolidation_strategy"] = strategy
        merged["content_fingerprint"] = existing.get(
            "content_fingerprint", incoming.get("content_fingerprint")
        )
        if "workspace" in incoming:
            merged["workspace"] = incoming["workspace"]
        return merged

    def _merge_source_snippets(
        self,
        existing: list[dict[str, Any]],
        incoming: list[dict[str, Any]],
    ) -> list[dict[str, str]]:
        by_fingerprint: dict[str, dict[str, str]] = {}

        for raw_snippet in [*existing, *incoming]:
            if not isinstance(raw_snippet, dict):
                continue
            fingerprint = str(raw_snippet.get("fingerprint") or "").strip()
            content = _normalize_text(str(raw_snippet.get("content") or ""))
            timestamp = str(raw_snippet.get("timestamp") or "")
            session_id = str(raw_snippet.get("session_id") or "")
            if not fingerprint or not content:
                continue
            snippet = self._build_source_snippet(
                content=content,
                timestamp=timestamp,
                session_id=session_id,
                fingerprint=fingerprint,
            )
            current = by_fingerprint.get(fingerprint)
            if current is None or timestamp >= current.get("timestamp", ""):
                by_fingerprint[fingerprint] = snippet

        ordered_keys = sorted(
            by_fingerprint,
            key=lambda key: by_fingerprint[key].get("timestamp", ""),
        )
        if self.max_cluster_snippets > 0 and len(ordered_keys) > self.max_cluster_snippets:
            ordered_keys = ordered_keys[-self.max_cluster_snippets :]
        return [by_fingerprint[key] for key in ordered_keys]

    def _build_cluster_summary(self, snippets: list[dict[str, str]]) -> dict[str, str]:
        contents = [
            _normalize_text(str(item.get("content") or ""))
            for item in snippets
            if isinstance(item, dict)
        ]
        contents = [value for value in contents if value]
        if not contents:
            return {"content": "", "summary_strategy": "single_snippet"}
        if len(contents) == 1:
            return {"content": contents[0], "summary_strategy": "single_snippet"}

        seen_segments: set[str] = set()
        segment_records: list[tuple[str, set[str], int]] = []
        for index, content in enumerate(contents):
            for segment in self._deduplicate_segments(content):
                signature = segment.casefold()
                if signature in seen_segments:
                    continue
                seen_segments.add(signature)
                segment_records.append((segment, set(_tokenize(segment)), index))

        def _priority(item: tuple[str, set[str], int]) -> tuple[int, int, int]:
            segment, tokens, index = item
            return (len(tokens), len(segment), index)

        covered_tokens: set[str] = set()
        selected: list[str] = []
        current_length = 0
        for segment, tokens, _ in sorted(segment_records, key=_priority, reverse=True):
            novelty = len(tokens - covered_tokens)
            if selected and novelty <= 0:
                continue
            extra = len(segment) if not selected else len(segment) + 1
            if selected and current_length + extra > self.max_content_chars:
                continue
            if not selected and len(segment) > self.max_content_chars:
                return {
                    "content": self._head_tail_truncate(segment),
                    "summary_strategy": "cluster_head_tail",
                }
            selected.append(segment)
            current_length += extra
            covered_tokens.update(tokens)

        if not selected:
            selected = self._deduplicate_segments(" ".join(contents))

        summary = _normalize_text(" ".join(selected))
        if len(summary) > self.max_content_chars:
            return {
                "content": self._head_tail_truncate(summary),
                "summary_strategy": "cluster_head_tail",
            }
        return {"content": summary, "summary_strategy": "cluster_summary"}

    async def _maybe_reconsolidate_document(
        self,
        document: dict[str, Any],
    ) -> dict[str, Any]:
        document_id = str(document.get("_id") or "")
        if not document_id:
            return {"merged": False, "deleted_id": None, "document": document}

        candidate = await self._find_cluster_candidate(document)
        if candidate is None:
            return {"merged": False, "deleted_id": None, "document": document}

        canonical, source = self._select_canonical_and_source(document, candidate)
        if canonical.get("_id") == source.get("_id"):
            return {"merged": False, "deleted_id": None, "document": document}

        merged = self._merge_documents(
            canonical,
            source,
            strategy="background_reconsolidation",
        )
        await self._upsert_document(merged)
        await self._upsert_vector(merged)
        await self._delete_document_and_vector(str(source.get("_id") or ""))
        return {
            "merged": True,
            "deleted_id": str(source.get("_id") or ""),
            "document": merged,
        }

    def _select_canonical_and_source(
        self,
        left: dict[str, Any],
        right: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        left_cluster_size = _safe_int(left.get("cluster_size"), 1)
        right_cluster_size = _safe_int(right.get("cluster_size"), 1)
        if right_cluster_size > left_cluster_size:
            return right, left
        if left_cluster_size > right_cluster_size:
            return left, right

        left_occurrence_count = _safe_int(left.get("occurrence_count"), 1)
        right_occurrence_count = _safe_int(right.get("occurrence_count"), 1)
        if right_occurrence_count > left_occurrence_count:
            return right, left
        if left_occurrence_count > right_occurrence_count:
            return left, right

        left_first_seen = str(left.get("first_seen_at") or "")
        right_first_seen = str(right.get("first_seen_at") or "")
        if right_first_seen and (not left_first_seen or right_first_seen < left_first_seen):
            return right, left
        return left, right

    async def _apply_aging_policy(self, document: dict[str, Any]) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        last_seen = self._parse_iso_datetime(str(document.get("last_seen_at") or ""))
        if last_seen is None:
            return {"updated": False, "deleted": False, "document": document}

        age_days = max(0.0, (now - last_seen).total_seconds() / 86400.0)
        occurrence_count = _safe_int(document.get("occurrence_count"), 1)
        cluster_size = _safe_int(document.get("cluster_size"), 1)

        if (
            age_days >= self.aging_delete_after_days
            and max(occurrence_count, cluster_size) < self.aging_keep_min_occurrences
        ):
            await self._delete_document_and_vector(str(document.get("_id") or ""))
            return {"updated": False, "deleted": True, "document": None}

        if age_days < self.aging_stale_after_days:
            if document.get("aging_state") not in {None, "active"}:
                updated = dict(document)
                updated["aging_state"] = "active"
                updated["last_aged_at"] = now.isoformat()
                await self._upsert_document(updated)
                await self._upsert_vector(updated)
                return {"updated": True, "deleted": False, "document": updated}
            return {"updated": False, "deleted": False, "document": document}

        stale_snippets = list(document.get("source_snippets") or [])
        if len(stale_snippets) > self.aging_max_snippets:
            stale_snippets = stale_snippets[-self.aging_max_snippets :]
        summary = self._build_cluster_summary(stale_snippets)

        if (
            document.get("aging_state") == "stale"
            and stale_snippets == list(document.get("source_snippets") or [])
            and summary["content"] == document.get("content")
            and summary["summary_strategy"] == document.get("summary_strategy")
        ):
            return {"updated": False, "deleted": False, "document": document}

        updated = dict(document)
        updated["source_snippets"] = stale_snippets
        updated["source_snippet_count"] = len(stale_snippets)
        updated["content"] = summary["content"]
        updated["summary_strategy"] = summary["summary_strategy"]
        updated["indexed_char_count"] = len(summary["content"])
        updated["compression_applied"] = True
        updated["compression_strategy"] = "aged_cluster_summary"
        updated["aging_state"] = "stale"
        updated["last_aged_at"] = now.isoformat()
        await self._upsert_document(updated)
        await self._upsert_vector(updated)
        return {"updated": True, "deleted": False, "document": updated}

    def _parse_iso_datetime(self, value: str) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    async def _delete_document_and_vector(self, document_id: str) -> None:
        if not document_id:
            return
        if self._mongo_collection is not None:
            delete_one = getattr(self._mongo_collection, "delete_one", None)
            if callable(delete_one):
                result = delete_one({"_id": document_id})
                if asyncio.iscoroutine(result):
                    await result
        if self._qdrant_client is not None and self._qdrant_models is not None:
            await asyncio.to_thread(
                self._qdrant_client.delete,
                collection_name=self._qdrant_collection_name,
                points_selector=self._qdrant_models.PointIdsList(points=[document_id]),
                wait=True,
            )

    async def _upsert_document(self, document: dict[str, Any]) -> None:
        if self._mongo_collection is None:
            return
        await self._mongo_collection.replace_one(
            {"_id": document["_id"]},
            document,
            upsert=True,
        )

    async def _upsert_vector(self, document: dict[str, Any]) -> None:
        if self._qdrant_client is None or self._qdrant_models is None:
            return

        vector = await self._embed_text(document["content"])
        payload = {
            "message_id": document["_id"],
            "user_id": document["user_id"],
            "session_ids": list(document.get("session_ids") or []),
            "role": document["role"],
            "timestamp": document["timestamp"],
            "last_seen_at": document.get("last_seen_at", document["timestamp"]),
            "occurrence_count": _safe_int(document.get("occurrence_count"), 1),
            "cluster_size": _safe_int(document.get("cluster_size"), 1),
            "consolidation_strategy": document.get("consolidation_strategy", "singleton"),
            "summary_strategy": document.get("summary_strategy", "single_snippet"),
        }
        workspace = document.get("workspace")
        if isinstance(workspace, str) and workspace.strip():
            payload["workspace"] = workspace.strip()

        point = self._qdrant_models.PointStruct(
            id=document["_id"],
            vector=vector,
            payload=payload,
        )
        await asyncio.to_thread(
            self._qdrant_client.upsert,
            collection_name=self._qdrant_collection_name,
            points=[point],
            wait=True,
        )

    async def _embed_text(self, text: str) -> list[float]:
        embedding = await self.embedding_func([text])
        vector = embedding[0]
        if hasattr(vector, "tolist"):
            return list(vector.tolist())
        return list(vector)

    async def _query_points(
        self,
        *,
        query: str,
        user_id: str,
        role: str | None = None,
        limit: int,
        exclude_session_id: str | None = None,
    ) -> list[Any]:
        if self._qdrant_client is None or self._qdrant_models is None:
            return []

        query_vector = await self._embed_text(query)
        models = self._qdrant_models
        must_conditions = [
            models.FieldCondition(
                key="user_id",
                match=models.MatchValue(value=user_id),
            )
        ]
        if role:
            must_conditions.append(
                models.FieldCondition(
                    key="role",
                    match=models.MatchValue(value=role),
                )
            )
        must_not_conditions = []
        if exclude_session_id:
            must_not_conditions.append(
                models.FieldCondition(
                    key="session_ids",
                    match=models.MatchValue(value=exclude_session_id),
                )
            )

        result = await asyncio.to_thread(
            self._qdrant_client.query_points,
            collection_name=self._qdrant_collection_name,
            query=query_vector,
            limit=limit,
            with_payload=True,
            query_filter=models.Filter(
                must=must_conditions,
                must_not=must_not_conditions,
            ),
        )
        return list(getattr(result, "points", []) or [])

    async def _search_vector_backend(
        self,
        user_id: str,
        *,
        query: str,
        limit: int,
        exclude_session_id: str | None,
    ) -> list[dict[str, Any]]:
        points = await self._query_points(
            query=query,
            user_id=user_id,
            limit=limit,
            exclude_session_id=exclude_session_id,
        )
        if not points:
            return []

        ranked_ids: list[str] = []
        score_by_id: dict[str, float] = {}
        for point in points:
            payload = getattr(point, "payload", {}) or {}
            message_id = payload.get("message_id") or str(getattr(point, "id", ""))
            if not message_id:
                continue
            ranked_ids.append(str(message_id))
            score_by_id[str(message_id)] = float(getattr(point, "score", 0.0) or 0.0)

        documents = await self._load_documents(ranked_ids)
        if not documents:
            return []

        results: list[dict[str, Any]] = []
        for message_id in ranked_ids:
            document = documents.get(message_id)
            if not document:
                continue
            session_ids = [
                value
                for value in list(document.get("session_ids") or [])
                if isinstance(value, str) and value.strip()
            ]
            if exclude_session_id and exclude_session_id in session_ids:
                continue
            results.append(
                {
                    "role": document.get("role", ""),
                    "content": document.get("content", ""),
                    "timestamp": document.get("timestamp", ""),
                    "score": score_by_id.get(message_id, 0.0),
                    "metadata": document.get("metadata", {}),
                    "session_id": session_ids[-1] if session_ids else None,
                    "session_ids": session_ids,
                    "user_id": document.get("user_id"),
                    "occurrence_count": _safe_int(document.get("occurrence_count"), 1),
                    "cluster_size": _safe_int(document.get("cluster_size"), 1),
                    "source_snippet_count": _safe_int(
                        document.get("source_snippet_count"), 1
                    ),
                    "first_seen_at": document.get("first_seen_at"),
                    "last_seen_at": document.get("last_seen_at"),
                    "compression_strategy": document.get("compression_strategy"),
                    "consolidation_strategy": document.get("consolidation_strategy"),
                    "summary_strategy": document.get("summary_strategy"),
                }
            )
        return results[:limit]

    async def _load_documents(self, message_ids: list[str]) -> dict[str, dict[str, Any]]:
        if not message_ids or self._mongo_collection is None:
            return {}

        cursor = self._mongo_collection.find({"_id": {"$in": list(message_ids)}})
        rows = await cursor.to_list(length=len(message_ids))
        results: dict[str, dict[str, Any]] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            identifier = row.get("_id")
            if identifier is None:
                continue
            results[str(identifier)] = row
        return results
