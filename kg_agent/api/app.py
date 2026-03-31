from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI

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
from kg_agent.api.agent_routes import create_agent_routes
from kg_agent.config import KGAgentConfig

load_dotenv(dotenv_path=".env", override=False)


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


def build_rag_from_env(*, workspace: str | None = None) -> LightRAG:
    llm_binding = get_env_value("LLM_BINDING", "openai", str)
    embedding_binding = get_env_value("EMBEDDING_BINDING", "openai", str)
    if llm_binding == "openai-ollama":
        llm_binding = "openai"
        embedding_binding = "ollama"

    llm_host = get_env_value("LLM_BINDING_HOST", get_default_host(llm_binding), str)
    embedding_host = get_env_value(
        "EMBEDDING_BINDING_HOST", get_default_host(embedding_binding), str
    )
    llm_api_key = get_env_value("LLM_BINDING_API_KEY", None)
    embedding_api_key = get_env_value("EMBEDDING_BINDING_API_KEY", None)
    llm_model = get_env_value("LLM_MODEL", "gpt-4o-mini", str)
    embedding_model = get_env_value("EMBEDDING_MODEL", None, special_none=True)
    embedding_dim = get_env_value("EMBEDDING_DIM", None, int, special_none=True)
    embedding_send_dim = get_env_value("EMBEDDING_SEND_DIM", False, bool)

    llm_timeout = get_env_value("LLM_TIMEOUT", DEFAULT_LLM_TIMEOUT, int)
    embedding_timeout = get_env_value(
        "EMBEDDING_TIMEOUT", DEFAULT_EMBEDDING_TIMEOUT, int
    )

    llm_model_func, llm_model_kwargs = _build_llm_model_func_from_env(
        llm_binding, llm_model, llm_host, llm_api_key, llm_timeout
    )
    embedding_func = _build_embedding_func_from_env(
        embedding_binding,
        embedding_model,
        embedding_host,
        embedding_api_key,
        embedding_timeout,
        embedding_dim,
        embedding_send_dim,
    )

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
        embedding_func=embedding_func,
        default_llm_timeout=llm_timeout,
        default_embedding_timeout=embedding_timeout,
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


def build_agent_core_from_env(
    *,
    rag: LightRAG | None = None,
    rag_provider=None,
) -> AgentCore:
    config = KGAgentConfig.from_env()
    if rag_provider is not None:
        return AgentCore(rag_provider=rag_provider, config=config)
    return AgentCore(rag=rag or build_rag_from_env(), config=config)


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

    if agent_core is None:
        if rag_instance is None:
            rag_provider = EnvLightRAGProvider(config=config_obj)
            manage_rag_lifecycle = True
            agent_core = AgentCore(rag_provider=rag_provider.get, config=config_obj)
        else:
            agent_core = AgentCore(rag=rag_instance, config=config_obj)
    elif rag_instance is None:
        rag_instance = getattr(agent_core, "_rag", None)
        rag_provider = getattr(agent_core, "_rag_provider", None)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            yield
        finally:
            if manage_rag_lifecycle and rag_provider is not None:
                await rag_provider.finalize_all()
            elif manage_rag_lifecycle and rag_instance is not None:
                await rag_instance.finalize_storages()
                finalize_share_data()

    app = FastAPI(
        title="KG Agent API",
        description="Business-layer agent API on top of LightRAG backend",
        version="0.1.0",
        lifespan=lifespan if manage_rag_lifecycle else None,
    )
    app.state.rag = rag_instance
    app.state.rag_provider = rag_provider
    app.state.agent_core = agent_core
    app.include_router(create_agent_routes(agent_core))

    @app.get("/health")
    async def health():
        health_workspace = getattr(rag_instance, "workspace", None)
        if health_workspace is None and rag_provider is not None:
            health_workspace = rag_provider.default_workspace
        return {
            "status": "ok",
            "service": "kg_agent",
            "workspace": health_workspace,
            "rag_bootstrapped": bool(rag_instance is not None or rag_provider is not None),
            "dynamic_workspace_enabled": bool(rag_provider is not None),
            "active_workspaces": (
                rag_provider.list_active_workspaces() if rag_provider is not None else []
            ),
        }

    return app


def main() -> None:
    import uvicorn

    host = get_env_value("KG_AGENT_HOST", get_env_value("HOST", "0.0.0.0", str), str)
    port = get_env_value("KG_AGENT_PORT", 9721, int)
    uvicorn.run("kg_agent.api.app:create_app", host=host, port=port, factory=True)


if __name__ == "__main__":
    main()
