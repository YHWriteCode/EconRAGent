from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import AsyncIterator
from typing import Any

from dotenv import load_dotenv

load_dotenv(dotenv_path=".env", override=False)
logger = logging.getLogger(__name__)


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _first_env_value(*names: str) -> str:
    for name in names:
        value = os.getenv(name)
        if value is None:
            continue
        normalized = value.strip()
        if normalized:
            return normalized
    return ""


def _first_env_float(default: float, *names: str) -> float:
    for name in names:
        value = os.getenv(name)
        if value is None:
            continue
        try:
            return float(value)
        except ValueError:
            continue
    return default


@dataclass
class AgentModelConfig:
    provider: str = "openai_compatible"
    model_name: str = ""
    base_url: str = ""
    api_key: str = ""
    timeout_s: float = 60.0

    @classmethod
    def from_env_keys(
        cls,
        *,
        provider_keys: tuple[str, ...],
        model_keys: tuple[str, ...],
        base_url_keys: tuple[str, ...],
        api_key_keys: tuple[str, ...],
        timeout_keys: tuple[str, ...],
        default_provider: str = "openai_compatible",
        default_timeout: float = 60.0,
    ) -> "AgentModelConfig":
        return cls(
            provider=_first_env_value(*provider_keys) or default_provider,
            model_name=_first_env_value(*model_keys),
            base_url=_first_env_value(*base_url_keys),
            api_key=_first_env_value(*api_key_keys),
            timeout_s=_first_env_float(default_timeout, *timeout_keys),
        )

    @classmethod
    def from_env(cls) -> "AgentModelConfig":
        return cls.from_env_keys(
            provider_keys=("KG_AGENT_MODEL_PROVIDER",),
            model_keys=("KG_AGENT_MODEL_NAME", "LLM_MODEL", "OPENAI_MODEL"),
            base_url_keys=(
                "KG_AGENT_MODEL_BASE_URL",
                "OPENAI_BASE_URL",
                "LLM_BINDING_HOST",
            ),
            api_key_keys=(
                "KG_AGENT_MODEL_API_KEY",
                "OPENAI_API_KEY",
                "LLM_BINDING_API_KEY",
            ),
            timeout_keys=("KG_AGENT_MODEL_TIMEOUT_S",),
        )

    @classmethod
    def from_utility_env(cls) -> "AgentModelConfig":
        return cls.from_env_keys(
            provider_keys=(
                "KG_AGENT_UTILITY_MODEL_PROVIDER",
                "UTILITY_LLM_PROVIDER",
            ),
            model_keys=(
                "KG_AGENT_UTILITY_MODEL_NAME",
                "UTILITY_LLM_MODEL",
            ),
            base_url_keys=(
                "KG_AGENT_UTILITY_MODEL_BASE_URL",
                "UTILITY_LLM_BINDING_HOST",
            ),
            api_key_keys=(
                "KG_AGENT_UTILITY_MODEL_API_KEY",
                "UTILITY_LLM_BINDING_API_KEY",
            ),
            timeout_keys=(
                "KG_AGENT_UTILITY_MODEL_TIMEOUT_S",
                "UTILITY_LLM_TIMEOUT",
            ),
        )

    def is_configured(self) -> bool:
        return bool(
            self.model_name
            and self.base_url
            and self.provider in {"openai_compatible", "openai"}
        )


@dataclass
class ToolConfig:
    enable_web_search: bool = False
    enable_memory: bool = True
    enable_quant: bool = True
    enable_kg_ingest: bool = True

    @classmethod
    def from_env(cls) -> "ToolConfig":
        return cls(
            enable_web_search=_env_bool("KG_AGENT_ENABLE_WEB_SEARCH", False),
            enable_memory=_env_bool("KG_AGENT_ENABLE_MEMORY", True),
            enable_quant=_env_bool("KG_AGENT_ENABLE_QUANT", True),
            enable_kg_ingest=_env_bool("KG_AGENT_ENABLE_KG_INGEST", True),
        )


@dataclass
class CrawlerConfig:
    provider: str = "crawl4ai"
    browser_type: str = "chromium"
    browser_channel: str = ""
    search_engine: str = "duckduckgo"
    headless: bool = True
    verbose: bool = False
    cache_mode: str = "BYPASS"
    max_pages: int = 3
    max_content_chars: int = 4000
    word_count_threshold: int = 20
    page_timeout_ms: int = 30000

    @classmethod
    def from_env(cls) -> "CrawlerConfig":
        return cls(
            provider=os.getenv("KG_AGENT_WEB_CRAWLER_PROVIDER", "crawl4ai"),
            browser_type=os.getenv("KG_AGENT_WEB_CRAWLER_BROWSER_TYPE", "chromium"),
            browser_channel=os.getenv("KG_AGENT_WEB_CRAWLER_BROWSER_CHANNEL", "").strip(),
            search_engine=os.getenv("KG_AGENT_WEB_SEARCH_ENGINE", "duckduckgo"),
            headless=_env_bool("KG_AGENT_WEB_CRAWLER_HEADLESS", True),
            verbose=_env_bool("KG_AGENT_WEB_CRAWLER_VERBOSE", False),
            cache_mode=os.getenv("KG_AGENT_WEB_CRAWLER_CACHE_MODE", "BYPASS"),
            max_pages=_env_int("KG_AGENT_WEB_CRAWLER_MAX_PAGES", 3),
            max_content_chars=_env_int("KG_AGENT_WEB_CRAWLER_MAX_CONTENT_CHARS", 4000),
            word_count_threshold=_env_int(
                "KG_AGENT_WEB_CRAWLER_WORD_COUNT_THRESHOLD", 20
            ),
            page_timeout_ms=_env_int(
                "KG_AGENT_WEB_CRAWLER_PAGE_TIMEOUT_MS", 30000
            ),
        )


@dataclass
class SchedulerConfig:
    enable_scheduler: bool = False
    check_interval_seconds: int = 60
    sources_file: str = ""
    state_file: str = "scheduler_state.json"
    coordination_backend: str = "auto"
    coordination_redis_url: str = ""
    coordination_ttl_seconds: int = 120
    enable_leader_election: bool = False
    loop_lease_key: str = "scheduler:loop"

    @classmethod
    def from_env(cls) -> "SchedulerConfig":
        return cls(
            enable_scheduler=_env_bool("KG_AGENT_ENABLE_SCHEDULER", False),
            check_interval_seconds=_env_int(
                "KG_AGENT_SCHEDULER_CHECK_INTERVAL", 60
            ),
            sources_file=os.getenv("KG_AGENT_SCHEDULER_SOURCES_FILE", "").strip(),
            state_file=os.getenv(
                "KG_AGENT_SCHEDULER_STATE_FILE", "scheduler_state.json"
            ).strip()
            or "scheduler_state.json",
            coordination_backend=os.getenv(
                "KG_AGENT_SCHEDULER_COORDINATION_BACKEND", "auto"
            ).strip()
            or "auto",
            coordination_redis_url=os.getenv(
                "KG_AGENT_SCHEDULER_COORDINATION_REDIS_URL",
                os.getenv("REDIS_URI", ""),
            ).strip(),
            coordination_ttl_seconds=_env_int(
                "KG_AGENT_SCHEDULER_COORDINATION_TTL_SECONDS", 120
            ),
            enable_leader_election=_env_bool(
                "KG_AGENT_SCHEDULER_ENABLE_LEADER_ELECTION", False
            ),
            loop_lease_key=os.getenv(
                "KG_AGENT_SCHEDULER_LOOP_LEASE_KEY", "scheduler:loop"
            ).strip()
            or "scheduler:loop",
        )


@dataclass
class FreshnessConfig:
    threshold_seconds: int = 604800
    enable_auto_ingest: bool = False
    staleness_decay_days: float = 7.0
    enable_freshness_decay: bool = False

    @classmethod
    def from_env(cls) -> "FreshnessConfig":
        return cls(
            threshold_seconds=_env_int(
                "KG_AGENT_FRESHNESS_THRESHOLD_SECONDS", 604800
            ),
            enable_auto_ingest=_env_bool("KG_AGENT_ENABLE_AUTO_INGEST", False),
            staleness_decay_days=_env_float(
                "KG_AGENT_STALENESS_DECAY_DAYS", 7.0
            ),
            enable_freshness_decay=_env_bool(
                "KG_AGENT_ENABLE_FRESHNESS_DECAY", False
            ),
        )


@dataclass
class PersistenceConfig:
    memory_backend: str = "memory"
    memory_sqlite_path: str = "kg_agent_memory.sqlite3"
    memory_mongo_collection: str = "kg_agent_conversation_messages"
    user_profile_backend: str = "memory"
    user_profile_sqlite_path: str = "kg_agent_profiles.sqlite3"
    user_profile_mongo_collection: str = "kg_agent_user_profiles"
    scheduler_store_backend: str = "json"
    scheduler_store_sqlite_path: str = "kg_agent_scheduler.sqlite3"
    cross_session_backend: str = "memory"
    cross_session_mongo_collection: str = "kg_agent_cross_session_messages"
    cross_session_qdrant_collection_prefix: str = "kg_agent_cross_session"
    cross_session_min_content_chars: int = 8
    cross_session_max_content_chars: int = 1200
    cross_session_max_session_refs: int = 12
    cross_session_enable_consolidation: bool = True
    cross_session_consolidation_similarity_threshold: float = 0.82
    cross_session_consolidation_top_k: int = 3
    cross_session_max_cluster_snippets: int = 6
    cross_session_enable_background_maintenance: bool = False
    cross_session_maintenance_interval_seconds: int = 1800
    cross_session_maintenance_batch_size: int = 100
    cross_session_aging_stale_after_days: float = 14.0
    cross_session_aging_delete_after_days: float = 60.0
    cross_session_aging_keep_min_occurrences: int = 2
    cross_session_aging_max_snippets: int = 3

    @classmethod
    def from_env(cls) -> "PersistenceConfig":
        return cls(
            memory_backend=os.getenv("KG_AGENT_MEMORY_BACKEND", "memory").strip()
            or "memory",
            memory_sqlite_path=os.getenv(
                "KG_AGENT_MEMORY_SQLITE_PATH", "kg_agent_memory.sqlite3"
            ).strip()
            or "kg_agent_memory.sqlite3",
            memory_mongo_collection=os.getenv(
                "KG_AGENT_MEMORY_MONGO_COLLECTION",
                "kg_agent_conversation_messages",
            ).strip()
            or "kg_agent_conversation_messages",
            user_profile_backend=os.getenv(
                "KG_AGENT_USER_PROFILE_BACKEND", "memory"
            ).strip()
            or "memory",
            user_profile_sqlite_path=os.getenv(
                "KG_AGENT_USER_PROFILE_SQLITE_PATH", "kg_agent_profiles.sqlite3"
            ).strip()
            or "kg_agent_profiles.sqlite3",
            user_profile_mongo_collection=os.getenv(
                "KG_AGENT_USER_PROFILE_MONGO_COLLECTION",
                "kg_agent_user_profiles",
            ).strip()
            or "kg_agent_user_profiles",
            scheduler_store_backend=os.getenv(
                "KG_AGENT_SCHEDULER_STORE_BACKEND", "json"
            ).strip()
            or "json",
            scheduler_store_sqlite_path=os.getenv(
                "KG_AGENT_SCHEDULER_STORE_SQLITE_PATH",
                "kg_agent_scheduler.sqlite3",
            ).strip()
            or "kg_agent_scheduler.sqlite3",
            cross_session_backend=os.getenv(
                "KG_AGENT_CROSS_SESSION_BACKEND", "memory"
            ).strip()
            or "memory",
            cross_session_mongo_collection=os.getenv(
                "KG_AGENT_CROSS_SESSION_MONGO_COLLECTION",
                "kg_agent_cross_session_messages",
            ).strip()
            or "kg_agent_cross_session_messages",
            cross_session_qdrant_collection_prefix=os.getenv(
                "KG_AGENT_CROSS_SESSION_QDRANT_COLLECTION_PREFIX",
                "kg_agent_cross_session",
            ).strip()
            or "kg_agent_cross_session",
            cross_session_min_content_chars=_env_int(
                "KG_AGENT_CROSS_SESSION_MIN_CONTENT_CHARS", 8
            ),
            cross_session_max_content_chars=_env_int(
                "KG_AGENT_CROSS_SESSION_MAX_CONTENT_CHARS", 1200
            ),
            cross_session_max_session_refs=_env_int(
                "KG_AGENT_CROSS_SESSION_MAX_SESSION_REFS", 12
            ),
            cross_session_enable_consolidation=_env_bool(
                "KG_AGENT_CROSS_SESSION_ENABLE_CONSOLIDATION", True
            ),
            cross_session_consolidation_similarity_threshold=_env_float(
                "KG_AGENT_CROSS_SESSION_CONSOLIDATION_SIMILARITY_THRESHOLD", 0.82
            ),
            cross_session_consolidation_top_k=_env_int(
                "KG_AGENT_CROSS_SESSION_CONSOLIDATION_TOP_K", 3
            ),
            cross_session_max_cluster_snippets=_env_int(
                "KG_AGENT_CROSS_SESSION_MAX_CLUSTER_SNIPPETS", 6
            ),
            cross_session_enable_background_maintenance=_env_bool(
                "KG_AGENT_CROSS_SESSION_ENABLE_BACKGROUND_MAINTENANCE", False
            ),
            cross_session_maintenance_interval_seconds=_env_int(
                "KG_AGENT_CROSS_SESSION_MAINTENANCE_INTERVAL_SECONDS", 1800
            ),
            cross_session_maintenance_batch_size=_env_int(
                "KG_AGENT_CROSS_SESSION_MAINTENANCE_BATCH_SIZE", 100
            ),
            cross_session_aging_stale_after_days=_env_float(
                "KG_AGENT_CROSS_SESSION_AGING_STALE_AFTER_DAYS", 14.0
            ),
            cross_session_aging_delete_after_days=_env_float(
                "KG_AGENT_CROSS_SESSION_AGING_DELETE_AFTER_DAYS", 60.0
            ),
            cross_session_aging_keep_min_occurrences=_env_int(
                "KG_AGENT_CROSS_SESSION_AGING_KEEP_MIN_OCCURRENCES", 2
            ),
            cross_session_aging_max_snippets=_env_int(
                "KG_AGENT_CROSS_SESSION_AGING_MAX_SNIPPETS", 3
            ),
        )


@dataclass
class AgentRuntimeConfig:
    default_workspace: str = ""
    default_domain_schema: str = "general"
    max_iterations: int = 3
    memory_window_turns: int = 6
    debug: bool = False

    @classmethod
    def from_env(cls) -> "AgentRuntimeConfig":
        return cls(
            default_workspace=os.getenv(
                "KG_AGENT_DEFAULT_WORKSPACE", os.getenv("WORKSPACE", "")
            ),
            default_domain_schema=os.getenv(
                "KG_AGENT_DEFAULT_DOMAIN_SCHEMA", "general"
            ),
            max_iterations=_env_int("KG_AGENT_MAX_ITERATIONS", 3),
            memory_window_turns=_env_int("KG_AGENT_MEMORY_WINDOW_TURNS", 6),
            debug=_env_bool("KG_AGENT_DEBUG", False),
        )


@dataclass
class KGAgentConfig:
    agent_model: AgentModelConfig = field(default_factory=AgentModelConfig)
    utility_model: AgentModelConfig = field(default_factory=AgentModelConfig)
    tool_config: ToolConfig = field(default_factory=ToolConfig)
    crawler: CrawlerConfig = field(default_factory=CrawlerConfig)
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    freshness: FreshnessConfig = field(default_factory=FreshnessConfig)
    persistence: PersistenceConfig = field(default_factory=PersistenceConfig)
    runtime: AgentRuntimeConfig = field(default_factory=AgentRuntimeConfig)
    judge_model: AgentModelConfig | None = None
    answer_model: AgentModelConfig | None = None
    path_model: AgentModelConfig | None = None

    @classmethod
    def from_env(cls) -> "KGAgentConfig":
        return cls(
            agent_model=AgentModelConfig.from_env(),
            utility_model=AgentModelConfig.from_utility_env(),
            tool_config=ToolConfig.from_env(),
            crawler=CrawlerConfig.from_env(),
            scheduler=SchedulerConfig.from_env(),
            freshness=FreshnessConfig.from_env(),
            persistence=PersistenceConfig.from_env(),
            runtime=AgentRuntimeConfig.from_env(),
        )


class AgentLLMClient:
    """OpenAI-compatible LLM client for the business-layer agent."""

    def __init__(self, config: AgentModelConfig):
        self.config = config
        self._client: Any | None = None
        self._disabled_reason: str | None = None

    def is_available(self) -> bool:
        return self.config.is_configured() and self.config.provider != "disabled"

    @property
    def disabled_reason(self) -> str | None:
        return self._disabled_reason

    def _ensure_client(self) -> Any:
        if not self.is_available():
            self._disabled_reason = "Agent model is not configured"
            raise RuntimeError(self._disabled_reason)

        if self._client is not None:
            return self._client

        if self.config.provider not in {"openai_compatible", "openai"}:
            self._disabled_reason = (
                f"Unsupported KG agent model provider: {self.config.provider}"
            )
            raise RuntimeError(self._disabled_reason)

        try:
            from openai import AsyncOpenAI
        except ImportError as exc:
            self._disabled_reason = "openai package is not installed"
            raise RuntimeError(self._disabled_reason) from exc

        self._client = AsyncOpenAI(
            api_key=self.config.api_key or "EMPTY",
            base_url=self.config.base_url,
            timeout=self.config.timeout_s,
        )
        return self._client

    async def complete_text(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.2,
        max_tokens: int = 1200,
    ) -> str:
        client = self._ensure_client()
        response = await client.chat.completions.create(
            model=self.config.model_name,
            temperature=temperature,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        message = response.choices[0].message
        return message.content or ""

    async def stream_text(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.2,
        max_tokens: int = 1200,
    ) -> AsyncIterator[str]:
        client = self._ensure_client()
        stream = await client.chat.completions.create(
            model=self.config.model_name,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=True,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        async for chunk in stream:
            try:
                delta = chunk.choices[0].delta.content
            except (AttributeError, IndexError):
                delta = None
            if isinstance(delta, str) and delta:
                yield delta

    async def complete_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.0,
        max_tokens: int = 800,
    ) -> dict[str, Any]:
        raw_text = await self.complete_text(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        payload = raw_text.strip()
        if payload.startswith("```"):
            payload = payload.strip("`")
            if payload.lower().startswith("json"):
                payload = payload[4:].strip()
        start = payload.find("{")
        end = payload.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise ValueError(f"LLM did not return a JSON object: {raw_text}")
        return json.loads(payload[start : end + 1])


class FallbackLLMClient:
    """Prefer a lightweight utility client and fall back to the main client when needed."""

    def __init__(
        self,
        *,
        primary: AgentLLMClient | None,
        fallback: AgentLLMClient | None,
        label: str,
    ):
        self.primary = primary
        self.fallback = fallback
        self.label = label
        self._warned_primary_unavailable = False

    def is_available(self) -> bool:
        return bool(
            (self.primary is not None and self.primary.is_available())
            or (self.fallback is not None and self.fallback.is_available())
        )

    def _resolve_client(self) -> AgentLLMClient:
        if self.primary is not None and self.primary.is_available():
            return self.primary
        if self.fallback is not None and self.fallback.is_available():
            if not self._warned_primary_unavailable:
                logger.warning(
                    "Utility model is not configured for %s; falling back to the main agent model.",
                    self.label,
                )
                self._warned_primary_unavailable = True
            return self.fallback
        raise RuntimeError(
            f"No LLM client is available for {self.label}. Configure the utility model or the main agent model."
        )

    async def complete_text(self, **kwargs) -> str:
        client = self._resolve_client()
        return await client.complete_text(**kwargs)

    async def stream_text(self, **kwargs) -> AsyncIterator[str]:
        client = self._resolve_client()
        async for chunk in client.stream_text(**kwargs):
            yield chunk

    async def complete_json(self, **kwargs) -> dict[str, Any]:
        client = self._resolve_client()
        return await client.complete_json(**kwargs)
