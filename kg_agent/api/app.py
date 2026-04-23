from __future__ import annotations

import asyncio
import inspect
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from lightrag_fork import LightRAG
from lightrag_fork.api.config import get_default_host
from lightrag_fork.constants import (
    DEFAULT_COSINE_THRESHOLD,
    DEFAULT_EMBEDDING_TIMEOUT,
    DEFAULT_LLM_TIMEOUT,
    DEFAULT_MAX_ASYNC,
    DEFAULT_SUMMARY_CONTEXT_SIZE,
    DEFAULT_SUMMARY_MAX_TOKENS,
)
from lightrag_fork.kg.shared_storage import finalize_share_data
from lightrag_fork.utils import EmbeddingFunc, get_env_value

from kg_agent.agent.agent_core import AgentCore
from kg_agent.api.agent_routes import build_error_envelope, create_agent_routes
from kg_agent.api.webui_routes import create_webui_routes
from kg_agent.config import KGAgentConfig
from kg_agent.crawler.crawler_adapter import Crawl4AIAdapter
from kg_agent.crawler.crawl_state_store import build_crawl_state_store
from kg_agent.crawler.scheduler import IngestScheduler, build_scheduler_coordinator
from kg_agent.crawler.source_registry import build_source_registry
from kg_agent.mcp.adapter import MCPAdapter
from kg_agent.memory.conversation_memory import ConversationMemoryStore
from kg_agent.memory.cross_session_store import CrossSessionStore
from kg_agent.memory.user_profile import UserProfileStore
from kg_agent.uploads import UploadStore
from kg_agent.workspace_registry import WorkspaceRecord, build_workspace_registry

load_dotenv(dotenv_path=".env", override=False)
logger = logging.getLogger(__name__)


class EnvLightRAGProvider:
    def __init__(self, *, config: KGAgentConfig | None = None):
        self.config = config or KGAgentConfig.from_env()
        self._instances: dict[str, LightRAG] = {}
        self._lock = asyncio.Lock()

    @property
    def default_workspace(self) -> str:
        return self.config.runtime.default_workspace or ""

    def _resolve_workspace(self, workspace: str | None) -> str:
        normalized = (workspace or "").strip()
        return normalized or self.default_workspace

    async def get(self, workspace: str | None = None) -> LightRAG:
        resolved_workspace = self._resolve_workspace(workspace)
        cached = self._instances.get(resolved_workspace)
        if cached is not None:
            return cached

        async with self._lock:
            cached = self._instances.get(resolved_workspace)
            if cached is not None:
                return cached

            rag = build_rag_from_env(workspace=resolved_workspace)
            await rag.initialize_storages()
            await rag.check_and_migrate_data()
            self._instances[resolved_workspace] = rag
            return rag

    def list_active_workspaces(self) -> list[str]:
        return sorted(self._instances)

    async def evict_workspace(self, workspace: str | None) -> None:
        resolved_workspace = self._resolve_workspace(workspace)
        rag = self._instances.pop(resolved_workspace, None)
        if rag is not None:
            await rag.finalize_storages()

    async def drop_workspace(self, workspace: str | None) -> None:
        resolved_workspace = self._resolve_workspace(workspace)
        rag = await self.get(resolved_workspace)
        storages = [
            getattr(rag, "full_docs", None),
            getattr(rag, "text_chunks", None),
            getattr(rag, "full_entities", None),
            getattr(rag, "full_relations", None),
            getattr(rag, "entity_chunks", None),
            getattr(rag, "relation_chunks", None),
            getattr(rag, "entities_vdb", None),
            getattr(rag, "relationships_vdb", None),
            getattr(rag, "chunks_vdb", None),
            getattr(rag, "chunk_entity_relation_graph", None),
            getattr(rag, "doc_status", None),
        ]
        for storage in storages:
            drop_func = getattr(storage, "drop", None)
            if callable(drop_func):
                result = drop_func()
                if inspect.isawaitable(result):
                    await result
        await self.evict_workspace(resolved_workspace)

    async def finalize_all(self) -> None:
        instances = list(self._instances.values())
        self._instances.clear()
        for rag in instances:
            await rag.finalize_storages()
        finalize_share_data()


def _normalize_ollama_host(base_url: str) -> str:
    normalized = (base_url or "").strip().rstrip("/")
    if normalized.endswith("/v1"):
        return normalized[: -len("/v1")]
    return normalized


def _build_rerank_model_func_from_env():
    binding = (get_env_value("RERANK_BINDING", "null", str) or "null").strip().lower()
    min_rerank_score = get_env_value("MIN_RERANK_SCORE", 0.0, float)
    if binding in {"", "null", "none", "disabled", "false"}:
        return None, min_rerank_score

    from lightrag_fork.rerank import ali_rerank, cohere_rerank, jina_rerank

    rerank_functions = {
        "cohere": cohere_rerank,
        "jina": jina_rerank,
        "aliyun": ali_rerank,
    }
    selected_rerank_func = rerank_functions.get(binding)
    if selected_rerank_func is None:
        raise ValueError(f"Unsupported rerank binding: {binding}")

    rerank_model = get_env_value("RERANK_MODEL", None, special_none=True)
    rerank_binding_host = get_env_value(
        "RERANK_BINDING_HOST", None, special_none=True
    )
    rerank_binding_api_key = get_env_value("RERANK_BINDING_API_KEY", None)

    if rerank_model is None or rerank_binding_host is None:
        signature = inspect.signature(selected_rerank_func)
        if rerank_model is None and "model" in signature.parameters:
            default_model = signature.parameters["model"].default
            if default_model is not inspect.Parameter.empty:
                rerank_model = default_model
        if rerank_binding_host is None and "base_url" in signature.parameters:
            default_base_url = signature.parameters["base_url"].default
            if default_base_url is not inspect.Parameter.empty:
                rerank_binding_host = default_base_url

    async def rerank_model_func(
        query: str,
        documents: list[str],
        top_n: int | None = None,
        extra_body: dict | None = None,
    ):
        kwargs = {
            "query": query,
            "documents": documents,
            "top_n": top_n,
            "api_key": rerank_binding_api_key,
            "model": rerank_model,
            "base_url": rerank_binding_host,
        }
        if binding == "cohere":
            kwargs["enable_chunking"] = get_env_value(
                "RERANK_ENABLE_CHUNKING", False, bool
            )
            kwargs["max_tokens_per_doc"] = get_env_value(
                "RERANK_MAX_TOKENS_PER_DOC", 4096, int
            )
        return await selected_rerank_func(**kwargs, extra_body=extra_body)

    return rerank_model_func, min_rerank_score


def _build_llm_model_func_from_env(
    binding: str, model_name: str, base_url: str, api_key: str | None, timeout: int
):
    if binding == "ollama":
        base_url = _normalize_ollama_host(base_url)

    if binding == "azure_openai":
        from lightrag_fork.llm.openai import azure_openai_complete_if_cache

        async def azure_complete(
            prompt,
            system_prompt=None,
            history_messages=None,
            keyword_extraction=False,
            **kwargs,
        ):
            return await azure_openai_complete_if_cache(
                model_name,
                prompt,
                system_prompt=system_prompt,
                history_messages=history_messages or [],
                base_url=base_url,
                api_key=api_key,
                timeout=timeout,
                api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-08-01-preview"),
                keyword_extraction=keyword_extraction,
                **kwargs,
            )

        return azure_complete, {}

    if binding == "gemini":
        from lightrag_fork.llm.gemini import gemini_complete_if_cache

        async def gemini_complete(
            prompt,
            system_prompt=None,
            history_messages=None,
            keyword_extraction=False,
            **kwargs,
        ):
            return await gemini_complete_if_cache(
                model_name,
                prompt,
                system_prompt=system_prompt,
                history_messages=history_messages or [],
                base_url=base_url,
                api_key=api_key,
                timeout=timeout,
                keyword_extraction=keyword_extraction,
                **kwargs,
            )

        return gemini_complete, {}

    if binding == "ollama":
        from lightrag_fork.llm.ollama import ollama_model_complete

        llm_kwargs = {
            "host": base_url,
            "api_key": api_key,
            "timeout": timeout,
            "options": {},
        }
        return ollama_model_complete, llm_kwargs

    from lightrag_fork.llm.openai import openai_complete_if_cache

    async def openai_like_complete(
        prompt,
        system_prompt=None,
        history_messages=None,
        keyword_extraction=False,
        **kwargs,
    ):
        return await openai_complete_if_cache(
            model_name,
            prompt,
            system_prompt=system_prompt,
            history_messages=history_messages or [],
            base_url=base_url,
            api_key=api_key,
            timeout=timeout,
            keyword_extraction=keyword_extraction,
            **kwargs,
        )

    return openai_like_complete, {}


def _build_optional_utility_llm_from_env():
    utility_binding = get_env_value(
        "UTILITY_LLM_BINDING",
        get_env_value("LLM_BINDING", "openai", str),
        str,
    )
    if utility_binding == "openai-ollama":
        utility_binding = "openai"

    utility_model = (
        get_env_value("KG_AGENT_UTILITY_MODEL_NAME", None, special_none=True)
        or get_env_value("UTILITY_LLM_MODEL", None, special_none=True)
    )
    utility_host = (
        get_env_value("KG_AGENT_UTILITY_MODEL_BASE_URL", None, special_none=True)
        or get_env_value("UTILITY_LLM_BINDING_HOST", None, special_none=True)
    )
    if not utility_model or not utility_host:
        return None, None

    utility_api_key = (
        get_env_value("KG_AGENT_UTILITY_MODEL_API_KEY", None)
        or get_env_value("UTILITY_LLM_BINDING_API_KEY", None)
    )
    utility_timeout = int(
        get_env_value(
            "KG_AGENT_UTILITY_MODEL_TIMEOUT_S",
            get_env_value("UTILITY_LLM_TIMEOUT", DEFAULT_LLM_TIMEOUT, float),
            float,
        )
    )
    utility_llm_func, _ = _build_llm_model_func_from_env(
        utility_binding,
        utility_model,
        utility_host,
        utility_api_key,
        utility_timeout,
    )
    return utility_llm_func, utility_model


def _resolve_embedding_provider(binding: str):
    if binding == "azure_openai":
        from lightrag_fork.llm.openai import azure_openai_embed

        return azure_openai_embed
    if binding == "ollama":
        from lightrag_fork.llm.ollama import ollama_embed

        return ollama_embed
    if binding == "gemini":
        from lightrag_fork.llm.gemini import gemini_embed

        return gemini_embed
    if binding == "jina":
        from lightrag_fork.llm.jina import jina_embed

        return jina_embed

    from lightrag_fork.llm.openai import openai_embed

    return openai_embed


def _build_embedding_func_from_env(
    binding: str,
    model_name: str | None,
    base_url: str,
    api_key: str | None,
    timeout: int,
    embedding_dim: int | None,
    send_dimensions: bool,
) -> EmbeddingFunc:
    if binding == "ollama":
        base_url = _normalize_ollama_host(base_url)

    provider = _resolve_embedding_provider(binding)
    actual_func = provider.func if isinstance(provider, EmbeddingFunc) else provider
    resolved_model_name = model_name or getattr(provider, "model_name", None)
    resolved_dim = embedding_dim or getattr(provider, "embedding_dim", None) or 1536
    resolved_max_token_size = getattr(provider, "max_token_size", None)

    async def embedding_wrapper(
        texts: list[str],
        embedding_dim: int | None = None,
        max_token_size: int | None = None,
    ):
        if binding == "azure_openai":
            kwargs = {
                "texts": texts,
                "model": resolved_model_name,
                "base_url": base_url,
                "api_key": api_key,
                "embedding_dim": embedding_dim,
                "api_version": os.getenv(
                    "AZURE_EMBEDDING_API_VERSION",
                    os.getenv("AZURE_OPENAI_API_VERSION", "2024-08-01-preview"),
                ),
            }
            return await actual_func(**kwargs)
        if binding == "ollama":
            kwargs = {
                "texts": texts,
                "embed_model": resolved_model_name or "bge-m3:latest",
                "host": base_url,
                "api_key": api_key,
                "timeout": timeout,
                "max_token_size": max_token_size,
                "options": {},
            }
            return await actual_func(**kwargs)
        if binding == "gemini":
            kwargs = {
                "texts": texts,
                "model": resolved_model_name,
                "base_url": base_url,
                "api_key": api_key,
                "embedding_dim": embedding_dim,
                "task_type": "RETRIEVAL_DOCUMENT",
            }
            return await actual_func(**kwargs)
        if binding == "jina":
            kwargs = {
                "texts": texts,
                "model": resolved_model_name,
                "base_url": base_url,
                "api_key": api_key,
                "embedding_dim": embedding_dim,
            }
            return await actual_func(**kwargs)

        kwargs = {
            "texts": texts,
            "model": resolved_model_name,
            "base_url": base_url,
            "api_key": api_key,
            "embedding_dim": embedding_dim,
            "max_token_size": max_token_size,
        }
        return await actual_func(**kwargs)

    if binding in {"jina", "gemini"}:
        effective_send_dimensions = True
    else:
        effective_send_dimensions = send_dimensions

    return EmbeddingFunc(
        embedding_dim=resolved_dim,
        func=embedding_wrapper,
        max_token_size=resolved_max_token_size,
        send_dimensions=effective_send_dimensions,
        model_name=resolved_model_name,
    )


def build_embedding_func_from_env() -> EmbeddingFunc:
    embedding_binding = get_env_value("EMBEDDING_BINDING", "openai", str)
    if get_env_value("LLM_BINDING", "openai", str) == "openai-ollama":
        embedding_binding = "ollama"

    embedding_host = get_env_value(
        "EMBEDDING_BINDING_HOST", get_default_host(embedding_binding), str
    )
    embedding_api_key = get_env_value("EMBEDDING_BINDING_API_KEY", None)
    embedding_model = get_env_value("EMBEDDING_MODEL", None, special_none=True)
    embedding_dim = get_env_value("EMBEDDING_DIM", None, int, special_none=True)
    embedding_send_dim = get_env_value("EMBEDDING_SEND_DIM", False, bool)
    embedding_timeout = get_env_value(
        "EMBEDDING_TIMEOUT", DEFAULT_EMBEDDING_TIMEOUT, int
    )
    return _build_embedding_func_from_env(
        embedding_binding,
        embedding_model,
        embedding_host,
        embedding_api_key,
        embedding_timeout,
        embedding_dim,
        embedding_send_dim,
    )


def build_rag_from_env(*, workspace: str | None = None) -> LightRAG:
    llm_binding = get_env_value("LLM_BINDING", "openai", str)
    if llm_binding == "openai-ollama":
        llm_binding = "openai"

    llm_host = get_env_value("LLM_BINDING_HOST", get_default_host(llm_binding), str)
    llm_api_key = get_env_value("LLM_BINDING_API_KEY", None)
    llm_model = get_env_value("LLM_MODEL", "gpt-4o-mini", str)

    llm_timeout = get_env_value("LLM_TIMEOUT", DEFAULT_LLM_TIMEOUT, int)
    embedding_timeout = get_env_value(
        "EMBEDDING_TIMEOUT", DEFAULT_EMBEDDING_TIMEOUT, int
    )

    llm_model_func, llm_model_kwargs = _build_llm_model_func_from_env(
        llm_binding, llm_model, llm_host, llm_api_key, llm_timeout
    )
    embedding_func = build_embedding_func_from_env()
    rerank_model_func, min_rerank_score = _build_rerank_model_func_from_env()
    utility_llm_model_func, utility_llm_model_name = _build_optional_utility_llm_from_env()

    rag = LightRAG(
        working_dir=get_env_value("WORKING_DIR", "./rag_storage", str),
        workspace=workspace
        if workspace is not None
        else get_env_value("WORKSPACE", "", str),
        llm_model_func=llm_model_func,
        llm_model_name=llm_model,
        llm_model_max_async=get_env_value("MAX_ASYNC", DEFAULT_MAX_ASYNC, int),
        summary_max_tokens=get_env_value(
            "SUMMARY_MAX_TOKENS", DEFAULT_SUMMARY_MAX_TOKENS, int
        ),
        summary_context_size=get_env_value(
            "SUMMARY_CONTEXT_SIZE", DEFAULT_SUMMARY_CONTEXT_SIZE, int
        ),
        chunk_token_size=get_env_value("CHUNK_SIZE", 1200, int),
        chunk_overlap_token_size=get_env_value("CHUNK_OVERLAP_SIZE", 100, int),
        llm_model_kwargs=llm_model_kwargs,
        utility_llm_model_func=utility_llm_model_func,
        utility_llm_model_name=utility_llm_model_name,
        embedding_func=embedding_func,
        default_llm_timeout=llm_timeout,
        default_embedding_timeout=embedding_timeout,
        rerank_model_func=rerank_model_func,
        min_rerank_score=min_rerank_score,
        kv_storage=get_env_value("LIGHTRAG_KV_STORAGE", "JsonKVStorage", str),
        graph_storage=get_env_value("LIGHTRAG_GRAPH_STORAGE", "NetworkXStorage", str),
        vector_storage=get_env_value(
            "LIGHTRAG_VECTOR_STORAGE", "NanoVectorDBStorage", str
        ),
        doc_status_storage=get_env_value(
            "LIGHTRAG_DOC_STATUS_STORAGE", "JsonDocStatusStorage", str
        ),
        vector_db_storage_cls_kwargs={
            "cosine_better_than_threshold": get_env_value(
                "COSINE_THRESHOLD", DEFAULT_COSINE_THRESHOLD, float
            )
        },
        enable_llm_cache_for_entity_extract=get_env_value(
            "ENABLE_LLM_CACHE_FOR_EXTRACT", True, bool
        ),
        enable_llm_cache=get_env_value("ENABLE_LLM_CACHE", True, bool),
        max_parallel_insert=get_env_value("MAX_PARALLEL_INSERT", 2, int),
        max_graph_nodes=get_env_value("MAX_GRAPH_NODES", 1000, int),
    )
    return rag


def build_cross_session_store_from_env(
    *,
    config: KGAgentConfig,
    conversation_memory: ConversationMemoryStore,
) -> CrossSessionStore:
    embedding_func = None
    if config.persistence.cross_session_backend.strip().lower() != "memory":
        embedding_func = build_embedding_func_from_env()
    return CrossSessionStore(
        conversation_memory=conversation_memory,
        backend=config.persistence.cross_session_backend,
        embedding_func=embedding_func,
        mongo_collection=config.persistence.cross_session_mongo_collection,
        qdrant_collection_prefix=config.persistence.cross_session_qdrant_collection_prefix,
        min_content_chars=config.persistence.cross_session_min_content_chars,
        max_content_chars=config.persistence.cross_session_max_content_chars,
        max_session_refs=config.persistence.cross_session_max_session_refs,
        enable_consolidation=config.persistence.cross_session_enable_consolidation,
        consolidation_similarity_threshold=(
            config.persistence.cross_session_consolidation_similarity_threshold
        ),
        consolidation_top_k=config.persistence.cross_session_consolidation_top_k,
        max_cluster_snippets=config.persistence.cross_session_max_cluster_snippets,
        enable_background_maintenance=(
            config.persistence.cross_session_enable_background_maintenance
        ),
        maintenance_interval_seconds=(
            config.persistence.cross_session_maintenance_interval_seconds
        ),
        maintenance_batch_size=config.persistence.cross_session_maintenance_batch_size,
        aging_stale_after_days=config.persistence.cross_session_aging_stale_after_days,
        aging_delete_after_days=config.persistence.cross_session_aging_delete_after_days,
        aging_keep_min_occurrences=(
            config.persistence.cross_session_aging_keep_min_occurrences
        ),
        aging_max_snippets=config.persistence.cross_session_aging_max_snippets,
    )


def build_agent_core_from_env(
    *,
    rag: LightRAG | None = None,
    rag_provider=None,
    crawler_adapter: Crawl4AIAdapter | None = None,
) -> AgentCore:
    config = KGAgentConfig.from_env()
    mcp_adapter = build_mcp_adapter_from_env(config=config)
    conversation_memory = ConversationMemoryStore(
        backend=config.persistence.memory_backend,
        sqlite_path=config.persistence.memory_sqlite_path,
        mongo_collection=config.persistence.memory_mongo_collection,
    )
    user_profile_store = UserProfileStore(
        backend=config.persistence.user_profile_backend,
        sqlite_path=config.persistence.user_profile_sqlite_path,
        mongo_collection=config.persistence.user_profile_mongo_collection,
    )
    upload_store = UploadStore(config.persistence.uploads_dir)
    cross_session_store = build_cross_session_store_from_env(
        config=config,
        conversation_memory=conversation_memory,
    )
    if rag_provider is not None:
        return AgentCore(
            rag_provider=rag_provider,
            config=config,
            mcp_adapter=mcp_adapter,
            crawler_adapter=crawler_adapter,
            conversation_memory=conversation_memory,
            cross_session_store=cross_session_store,
            user_profile_store=user_profile_store,
            upload_store=upload_store,
        )
    return AgentCore(
        rag=rag or build_rag_from_env(),
        config=config,
        mcp_adapter=mcp_adapter,
        crawler_adapter=crawler_adapter,
        conversation_memory=conversation_memory,
        cross_session_store=cross_session_store,
        user_profile_store=user_profile_store,
        upload_store=upload_store,
    )


def build_crawler_adapter_from_env(
    *, config: KGAgentConfig | None = None
) -> Crawl4AIAdapter | None:
    config_obj = config or KGAgentConfig.from_env()
    if not config_obj.tool_config.enable_web_search:
        return None
    return Crawl4AIAdapter(config=config_obj.crawler)


def build_mcp_adapter_from_env(*, config: KGAgentConfig | None = None) -> MCPAdapter | None:
    config_obj = config or KGAgentConfig.from_env()
    if not config_obj.mcp.is_configured():
        return None
    return MCPAdapter(config=config_obj.mcp)


def create_app(
    *,
    rag: LightRAG | None = None,
    agent_core: AgentCore | None = None,
    config: KGAgentConfig | None = None,
) -> FastAPI:
    manage_rag_lifecycle = False
    rag_instance = rag
    rag_provider = None
    config_obj = config or KGAgentConfig.from_env()
    crawler_adapter = None
    mcp_adapter = None
    scheduler = None
    workspace_registry = build_workspace_registry(
        backend=config_obj.persistence.scheduler_store_backend,
        file_path=config_obj.persistence.workspace_registry_file,
        sqlite_path=config_obj.persistence.workspace_registry_sqlite_path,
    )
    upload_store = UploadStore(config_obj.persistence.uploads_dir)

    if agent_core is None:
        crawler_adapter = build_crawler_adapter_from_env(config=config_obj)
        mcp_adapter = build_mcp_adapter_from_env(config=config_obj)
        conversation_memory = ConversationMemoryStore(
            backend=config_obj.persistence.memory_backend,
            sqlite_path=config_obj.persistence.memory_sqlite_path,
            mongo_collection=config_obj.persistence.memory_mongo_collection,
        )
        user_profile_store = UserProfileStore(
            backend=config_obj.persistence.user_profile_backend,
            sqlite_path=config_obj.persistence.user_profile_sqlite_path,
            mongo_collection=config_obj.persistence.user_profile_mongo_collection,
        )
        cross_session_store = build_cross_session_store_from_env(
            config=config_obj,
            conversation_memory=conversation_memory,
        )
        if rag_instance is None:
            rag_provider = EnvLightRAGProvider(config=config_obj)
            manage_rag_lifecycle = True
            agent_core = AgentCore(
                rag_provider=rag_provider.get,
                config=config_obj,
                mcp_adapter=mcp_adapter,
                crawler_adapter=crawler_adapter,
                conversation_memory=conversation_memory,
                cross_session_store=cross_session_store,
                user_profile_store=user_profile_store,
                upload_store=upload_store,
            )
        else:
            agent_core = AgentCore(
                rag=rag_instance,
                config=config_obj,
                mcp_adapter=mcp_adapter,
                crawler_adapter=crawler_adapter,
                conversation_memory=conversation_memory,
                cross_session_store=cross_session_store,
                user_profile_store=user_profile_store,
                upload_store=upload_store,
            )
    elif rag_instance is None:
        rag_instance = getattr(agent_core, "_rag", None)
        rag_provider = getattr(agent_core, "_rag_provider", None)
        crawler_adapter = getattr(agent_core, "crawler_adapter", None)
        mcp_adapter = getattr(agent_core, "mcp_adapter", None)
        upload_store = getattr(agent_core, "upload_store", None) or upload_store
    else:
        crawler_adapter = getattr(agent_core, "crawler_adapter", None)
        mcp_adapter = getattr(agent_core, "mcp_adapter", None)
        upload_store = getattr(agent_core, "upload_store", None) or upload_store

    if crawler_adapter is not None and agent_core is not None:
        scheduler = IngestScheduler(
            rag_provider=agent_core._resolve_rag,
            crawler_adapter=crawler_adapter,
            source_registry=build_source_registry(
                backend=config_obj.persistence.scheduler_store_backend,
                file_path=config_obj.scheduler.sources_file,
                sqlite_path=config_obj.persistence.scheduler_store_sqlite_path,
            ),
            state_store=build_crawl_state_store(
                backend=config_obj.persistence.scheduler_store_backend,
                file_path=config_obj.scheduler.state_file,
                sqlite_path=config_obj.persistence.scheduler_store_sqlite_path,
            ),
            enabled=config_obj.scheduler.enable_scheduler,
            check_interval_seconds=config_obj.scheduler.check_interval_seconds,
            coordinator=build_scheduler_coordinator(config_obj.scheduler),
            coordination_ttl_seconds=config_obj.scheduler.coordination_ttl_seconds,
            utility_llm_client=getattr(agent_core, "utility_llm_client", None),
            enable_leader_election=config_obj.scheduler.enable_leader_election,
            loop_lease_key=config_obj.scheduler.loop_lease_key,
        )
        agent_core.crawl_state_store = scheduler.state_store

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            default_workspace = (config_obj.runtime.default_workspace or "").strip()
            if default_workspace:
                existing_workspace = await workspace_registry.get_workspace(default_workspace)
                if existing_workspace is None:
                    await workspace_registry.upsert_workspace(
                        WorkspaceRecord(
                            workspace_id=default_workspace,
                            display_name=default_workspace,
                            description="Default workspace",
                            created_at=datetime.now().astimezone().isoformat(),
                            updated_at=datetime.now().astimezone().isoformat(),
                        )
                    )
            if scheduler is not None:
                await scheduler.start()
            if agent_core is not None:
                initialize_external = getattr(
                    agent_core, "initialize_external_capabilities", None
                )
                if callable(initialize_external):
                    result = initialize_external()
                    if asyncio.iscoroutine(result):
                        await result
                cross_session_store = getattr(agent_core, "cross_session_store", None)
                start_maintenance = getattr(
                    cross_session_store, "start_background_maintenance", None
                )
                if callable(start_maintenance):
                    result = start_maintenance()
                    if asyncio.iscoroutine(result):
                        await result
            yield
        finally:
            if scheduler is not None:
                await scheduler.stop()
            if crawler_adapter is not None:
                await crawler_adapter.close()
            if mcp_adapter is not None:
                await mcp_adapter.close()
            if agent_core is not None:
                for store_name in (
                    "cross_session_store",
                    "conversation_memory",
                    "user_profile_store",
                ):
                    store = getattr(agent_core, store_name, None)
                    close_method = getattr(store, "close", None)
                    if callable(close_method):
                        result = close_method()
                        if asyncio.iscoroutine(result):
                            await result
            if manage_rag_lifecycle and rag_provider is not None:
                await rag_provider.finalize_all()
            elif manage_rag_lifecycle and rag_instance is not None:
                await rag_instance.finalize_storages()
                finalize_share_data()

    def _expose_internal_errors() -> bool:
        return bool(
            config_obj.api.expose_internal_errors or config_obj.runtime.debug
        )

    def _build_readiness_checks() -> dict[str, bool]:
        return {
            "agent_core": bool(agent_core is not None),
            "rag": bool(rag_instance is not None or rag_provider is not None),
            "dynamic_workspace": bool(rag_provider is not None),
            "scheduler": bool(scheduler is not None),
            "mcp": bool(mcp_adapter is not None),
        }

    app = FastAPI(
        title="KG Agent API",
        description="Business-layer agent API on top of LightRAG backend",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config_obj.api.cors_origins or ["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.rag = rag_instance
    app.state.rag_provider = rag_provider
    app.state.crawler_adapter = crawler_adapter
    app.state.mcp_adapter = mcp_adapter
    app.state.agent_core = agent_core
    app.state.scheduler = scheduler
    app.state.workspace_registry = workspace_registry
    app.state.upload_store = upload_store
    app.state.config = config_obj
    if agent_core is not None and getattr(agent_core, "upload_store", None) is None:
        agent_core.upload_store = upload_store
    app.include_router(
        create_agent_routes(
            agent_core,
            scheduler=scheduler,
            expose_internal_errors=_expose_internal_errors(),
        )
    )
    app.include_router(
        create_webui_routes(
            agent_core,
            scheduler=scheduler,
            workspace_registry=workspace_registry,
            upload_store=upload_store,
            rag_provider=rag_provider,
        )
    )

    webui_dir = Path(__file__).resolve().parent / "webui"
    if webui_dir.exists():
        assets_dir = webui_dir / "assets"
        if assets_dir.exists():
            app.mount("/webui/assets", StaticFiles(directory=assets_dir), name="webui-assets")

        @app.get("/", include_in_schema=False)
        async def webui_root_redirect():
            return RedirectResponse(url="/webui/chat", status_code=307)

        @app.get("/webui", include_in_schema=False)
        async def serve_webui_index():
            index_file = webui_dir / "index.html"
            if not index_file.exists():
                raise HTTPException(status_code=503, detail="WebUI build is not available")
            return FileResponse(index_file)

        @app.get("/webui/{full_path:path}", include_in_schema=False)
        async def serve_webui(full_path: str):
            index_file = webui_dir / "index.html"
            if not index_file.exists():
                raise HTTPException(status_code=503, detail="WebUI build is not available")
            candidate = (webui_dir / full_path).resolve()
            try:
                candidate.relative_to(webui_dir.resolve())
            except ValueError:
                raise HTTPException(status_code=404, detail="Not found")
            if candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(index_file)

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request,
        exc: RequestValidationError,
    ):
        return JSONResponse(
            status_code=422,
            content=build_error_envelope(
                422,
                code="validation_error",
                detail="Request validation failed.",
                details=exc.errors(),
                expose_internal_errors=True,
            ),
        )

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException):
        return JSONResponse(
            status_code=exc.status_code,
            content=build_error_envelope(
                exc.status_code,
                detail=exc.detail,
                expose_internal_errors=_expose_internal_errors(),
            ),
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        logger.exception(
            "Unhandled exception while serving %s %s",
            request.method,
            request.url.path,
        )
        return JSONResponse(
            status_code=500,
            content=build_error_envelope(
                500,
                code="internal_error",
                detail=str(exc),
                expose_internal_errors=_expose_internal_errors(),
            ),
        )

    @app.get("/health")
    async def health():
        health_workspace = getattr(rag_instance, "workspace", None)
        default_workspace = config_obj.runtime.default_workspace or ""
        active_workspaces = []
        if health_workspace is None and rag_provider is not None:
            health_workspace = rag_provider.default_workspace
            active_workspaces = rag_provider.list_active_workspaces()
        elif rag_provider is not None:
            active_workspaces = rag_provider.list_active_workspaces()
        mcp_capability_count = 0
        if agent_core is not None:
            capability_registry = getattr(agent_core, "capability_registry", None)
            list_capabilities = getattr(capability_registry, "list_capabilities", None)
            if callable(list_capabilities):
                mcp_capability_count = sum(
                    1
                    for capability in list_capabilities()
                    if getattr(capability, "kind", "") == "external_mcp"
                    and bool(getattr(capability, "enabled", False))
                )
        return {
            "status": "ok",
            "service": "kg_agent",
            "workspace": health_workspace,
            "default_workspace": default_workspace,
            "rag_bootstrapped": bool(rag_instance is not None or rag_provider is not None),
            "dynamic_workspace_enabled": bool(rag_provider is not None),
            "active_workspaces": active_workspaces,
            "active_workspace_count": len(active_workspaces),
            "scheduler_configured": bool(scheduler is not None),
            "scheduler_enabled": bool(scheduler and scheduler.enabled),
            "scheduler_running": bool(
                scheduler is not None
                and scheduler._task is not None
                and not scheduler._task.done()
            ),
            "mcp_configured": bool(mcp_adapter is not None),
            "mcp_capability_count": mcp_capability_count,
            "cross_session_maintenance_enabled": bool(
                getattr(
                    getattr(agent_core, "cross_session_store", None),
                    "enable_background_maintenance",
                    False,
                )
            ),
            "cross_session_maintenance_running": bool(
                getattr(
                    getattr(agent_core, "cross_session_store", None),
                    "maintenance_running",
                    False,
                )
            ),
        }

    @app.get("/ready")
    async def ready():
        checks = _build_readiness_checks()
        is_ready = checks["agent_core"] and checks["rag"]
        payload = {
            "status": "ready" if is_ready else "not_ready",
            "service": "kg_agent",
            "checks": checks,
        }
        if is_ready:
            return payload
        return JSONResponse(status_code=503, content=payload)

    return app


def main() -> None:
    import uvicorn

    host = get_env_value("KG_AGENT_HOST", get_env_value("HOST", "0.0.0.0", str), str)
    port = get_env_value("KG_AGENT_PORT", 9721, int)
    uvicorn.run("kg_agent.api.app:create_app", host=host, port=port, factory=True)


if __name__ == "__main__":
    main()
