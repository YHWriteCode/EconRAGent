from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from collections import Counter
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


def _find_project_root() -> Path:
    current = Path(__file__).resolve()
    for candidate in (current.parent, *current.parents):
        if (candidate / "pyproject.toml").exists():
            return candidate
    return current.parents[3]


REPO_ROOT = _find_project_root()
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


TEST_DOCS: list[tuple[str, str]] = [
    (
        "schema_doc_01.txt",
        "BYD Co. expanded battery production in China after a green manufacturing subsidy and tax rebate "
        "policy reduced financing costs. The electric vehicle industry expected higher gross margin and "
        "revenue growth, while lithium carbonate prices moved lower.",
    ),
    (
        "schema_doc_02.txt",
        "JPMorgan Chase said the Federal Reserve policy outlook and U.S. Treasury yield movements affected "
        "bank net interest margin. The banking industry remained cautious even as credit demand improved in "
        "the United States.",
    ),
    (
        "schema_doc_03.txt",
        "Saudi Arabia's export policy supported Brent crude prices, helping Exxon Mobil improve cash flow and "
        "capital expenditure guidance. The global energy industry tracked oil demand and inflation indicators "
        "across the United States and China.",
    ),
]

ECONOMY_DOMAIN_TYPES = {
    "company",
    "industry",
    "metric",
    "policy",
    "event",
    "asset",
    "institution",
    "country",
}


def load_env_files() -> None:
    load_dotenv(REPO_ROOT / ".env", override=False)


def prepare_env_aliases() -> None:
    if not os.getenv("MONGO_URI") and os.getenv("MONGODB_URI"):
        os.environ["MONGO_URI"] = os.environ["MONGODB_URI"]
    if not os.getenv("MONGODB_URI") and os.getenv("MONGO_URI"):
        os.environ["MONGODB_URI"] = os.environ["MONGO_URI"]
    if not os.getenv("QDRANT_URL"):
        qdrant_host = os.getenv("QDRANT_HOST")
        qdrant_port = os.getenv("QDRANT_PORT", "6333")
        if qdrant_host:
            os.environ["QDRANT_URL"] = f"http://{qdrant_host}:{qdrant_port}"
    if not os.getenv("OPENAI_API_BASE") and os.getenv("LLM_BINDING_HOST"):
        os.environ["OPENAI_API_BASE"] = os.environ["LLM_BINDING_HOST"]
    if not os.getenv("OPENAI_API_KEY") and os.getenv("LLM_BINDING_API_KEY"):
        os.environ["OPENAI_API_KEY"] = os.environ["LLM_BINDING_API_KEY"]
    if not os.getenv("LLM_MODEL_NAME") and os.getenv("LLM_MODEL"):
        os.environ["LLM_MODEL_NAME"] = os.environ["LLM_MODEL"]


def _read_int_env(name: str, default: int | None = None) -> int | None:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return int(value)


def _require_env(keys: list[str]) -> None:
    missing = [key for key in keys if not os.getenv(key)]
    if missing:
        raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")


def _build_openai_compatible_llm_func(
    llm_base_url: str | None,
    llm_api_key: str | None,
    llm_model_name: str,
    llm_timeout: int | None = None,
    llm_defaults: dict[str, Any] | None = None,
):
    from lightrag_fork.llm.openai import openai_complete_if_cache
    from lightrag_fork.types import GPTKeywordExtractionFormat

    default_kwargs = dict(llm_defaults or {})

    async def wrapped_llm(
        prompt,
        system_prompt=None,
        history_messages=None,
        keyword_extraction=False,
        **kwargs,
    ) -> str:
        if history_messages is None:
            history_messages = []

        keyword_extraction = kwargs.pop("keyword_extraction", keyword_extraction)
        resolved_model_name = kwargs.pop("model", llm_model_name)
        resolved_base_url = kwargs.pop("base_url", llm_base_url)
        resolved_api_key = kwargs.pop("api_key", llm_api_key)
        resolved_timeout = kwargs.pop("timeout", llm_timeout)
        final_kwargs = {**default_kwargs, **kwargs}
        if keyword_extraction:
            final_kwargs["response_format"] = GPTKeywordExtractionFormat

        return await openai_complete_if_cache(
            resolved_model_name,
            prompt,
            system_prompt=system_prompt,
            history_messages=history_messages,
            base_url=resolved_base_url,
            api_key=resolved_api_key,
            timeout=resolved_timeout,
            keyword_extraction=keyword_extraction,
            **final_kwargs,
        )

    return wrapped_llm


def _build_embedding_func(
    embedding_base_url: str | None,
    embedding_api_key: str | None,
    embedding_model: str | None,
    embedding_dim: int | None,
):
    from lightrag_fork.llm.openai import openai_embed

    kwargs: dict[str, Any] = {}
    if embedding_base_url:
        kwargs["base_url"] = embedding_base_url
    if embedding_api_key:
        kwargs["api_key"] = embedding_api_key
    if embedding_model:
        kwargs["model"] = embedding_model

    actual_dim = embedding_dim or openai_embed.embedding_dim
    actual_model_name = embedding_model or openai_embed.model_name

    if kwargs:
        return replace(
            openai_embed,
            func=partial(openai_embed.func, **kwargs),
            embedding_dim=actual_dim,
            model_name=actual_model_name,
        )
    return replace(
        openai_embed,
        embedding_dim=actual_dim,
        model_name=actual_model_name,
    )


async def _wait_doc_processed(rag, doc_id: str, timeout_s: float = 180.0) -> dict[str, Any]:
    from lightrag_fork.base import DocStatus

    start = time.time()
    while True:
        processed = await rag.doc_status.get_docs_by_status(DocStatus.PROCESSED)
        if doc_id in processed:
            return processed[doc_id]

        failed = await rag.doc_status.get_docs_by_status(DocStatus.FAILED)
        if doc_id in failed:
            raise RuntimeError(f"Document {doc_id} failed: {failed[doc_id]}")

        if time.time() - start > timeout_s:
            raise TimeoutError(f"Document {doc_id} not processed within {timeout_s}s")
        await asyncio.sleep(0.5)


def _extract_node_name(node: dict[str, Any]) -> str:
    for key in ("entity_name", "id", "name", "entity_id"):
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "<unknown>"


def _extract_node_type(node: dict[str, Any]) -> str:
    value = node.get("entity_type")
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, list) and value:
        first = value[0]
        if isinstance(first, str) and first.strip():
            return first.strip()
    return "UNKNOWN"


async def _collect_entities(rag) -> dict[str, str]:
    nodes = await rag.chunk_entity_relation_graph.get_all_nodes()
    result: dict[str, str] = {}
    for node in nodes:
        name = _extract_node_name(node)
        result[name] = _extract_node_type(node)
    return dict(sorted(result.items(), key=lambda item: item[0].lower()))


async def _run_case(case_name: str, addon_params: dict[str, Any]) -> dict[str, Any]:
    from lightrag_fork import LightRAG

    workspace = f"schema_{case_name}_{int(time.time())}"
    doc_ids: list[str] = []

    llm_base_url = os.getenv("OPENAI_API_BASE")
    llm_api_key = os.getenv("OPENAI_API_KEY")
    llm_model_name = os.getenv("LLM_MODEL_NAME", "gpt-4o-mini")
    llm_timeout = _read_int_env("LLM_TIMEOUT")
    embedding_base_url = os.getenv("EMBEDDING_BINDING_HOST") or llm_base_url
    embedding_api_key = os.getenv("EMBEDDING_BINDING_API_KEY") or llm_api_key
    embedding_model = os.getenv("EMBEDDING_MODEL")
    embedding_dim = _read_int_env("EMBEDDING_DIM")

    rag = LightRAG(
        working_dir=str(REPO_ROOT / "lightrag_fork" / "tests" / "e2e" / "rag_storage"),
        workspace=workspace,
        kv_storage="RedisKVStorage",
        vector_storage="QdrantVectorDBStorage",
        graph_storage="Neo4JStorage",
        doc_status_storage="MongoDocStatusStorage",
        addon_params=addon_params,
        llm_model_func=_build_openai_compatible_llm_func(
            llm_base_url=llm_base_url,
            llm_api_key=llm_api_key,
            llm_model_name=llm_model_name,
            llm_timeout=llm_timeout,
            llm_defaults={"temperature": 0},
        ),
        llm_model_name=llm_model_name,
        llm_model_kwargs={"base_url": llm_base_url, "api_key": llm_api_key, "temperature": 0},
        embedding_func=_build_embedding_func(
            embedding_base_url,
            embedding_api_key,
            embedding_model,
            embedding_dim,
        ),
    )

    await rag.initialize_storages()
    try:
        for index, (file_name, text) in enumerate(TEST_DOCS, start=1):
            doc_id = f"{case_name}-doc-{index}-{int(time.time())}"
            doc_ids.append(doc_id)
            await rag.ainsert(text, ids=[doc_id], file_paths=[file_name])
            await _wait_doc_processed(rag, doc_id)

        entities = await _collect_entities(rag)
        type_counter = Counter(entities.values())
        return {
            "case_name": case_name,
            "workspace": workspace,
            "entities": entities,
            "type_counter": dict(sorted(type_counter.items())),
        }
    finally:
        for doc_id in doc_ids:
            try:
                await rag.adelete_by_doc_id(doc_id)
            except Exception:
                pass
        await rag.finalize_storages()


def _compare_results(
    general_result: dict[str, Any], economy_result: dict[str, Any]
) -> dict[str, Any]:
    general_entities = general_result["entities"]
    economy_entities = economy_result["entities"]

    changed_types: list[dict[str, str]] = []
    for name in sorted(set(general_entities) & set(economy_entities)):
        general_type = general_entities[name]
        economy_type = economy_entities[name]
        if general_type != economy_type:
            changed_types.append(
                {
                    "entity_name": name,
                    "general_type": general_type,
                    "economy_type": economy_type,
                }
            )

    economy_domain_hits = {
        name: entity_type
        for name, entity_type in economy_entities.items()
        if entity_type.strip().lower() in ECONOMY_DOMAIN_TYPES
    }

    return {
        "changed_types": changed_types,
        "economy_domain_hits": dict(sorted(economy_domain_hits.items())),
        "general_type_counter": general_result["type_counter"],
        "economy_type_counter": economy_result["type_counter"],
    }


def _print_case(result: dict[str, Any]) -> None:
    print(f"\n===== {result['case_name']} =====")
    print(f"workspace: {result['workspace']}")
    print("entity type counts:")
    for entity_type, count in result["type_counter"].items():
        print(f"  - {entity_type}: {count}")
    print("sample entities:")
    for name, entity_type in list(result["entities"].items())[:20]:
        print(f"  - {name} -> {entity_type}")


def _print_comparison(comparison: dict[str, Any]) -> None:
    print("\n===== comparison =====")
    print("general type counts:", comparison["general_type_counter"])
    print("economy type counts:", comparison["economy_type_counter"])

    print("\nchanged type assignments:")
    if comparison["changed_types"]:
        for item in comparison["changed_types"]:
            print(
                f"  - {item['entity_name']}: {item['general_type']} -> {item['economy_type']}"
            )
    else:
        print("  - none")

    print("\neconomy domain type hits:")
    if comparison["economy_domain_hits"]:
        for name, entity_type in comparison["economy_domain_hits"].items():
            print(f"  - {name} -> {entity_type}")
    else:
        print("  - none")


async def main() -> int:
    load_env_files()
    prepare_env_aliases()
    _require_env(
        [
            "REDIS_URI",
            "NEO4J_URI",
            "NEO4J_USERNAME",
            "NEO4J_PASSWORD",
            "QDRANT_URL",
            "MONGO_URI",
            "MONGO_DATABASE",
            "OPENAI_API_KEY",
            "OPENAI_API_BASE",
        ]
    )

    general_result = await _run_case(
        "general",
        addon_params={
            "domain_schema": {
                "enabled": False,
                "mode": "general",
                "profile_name": "general",
            }
        },
    )
    economy_result = await _run_case(
        "economy",
        addon_params={
            "domain_schema": {
                "enabled": True,
                "mode": "domain",
                "profile_name": "economy",
            }
        },
    )

    comparison = _compare_results(general_result, economy_result)
    _print_case(general_result)
    _print_case(economy_result)
    _print_comparison(comparison)

    output_path = Path(__file__).resolve().with_name("schema_compare_output.json")
    output_path.write_text(
        json.dumps(
            {
                "general": general_result,
                "economy": economy_result,
                "comparison": comparison,
                "sample_docs": [
                    {"file_name": file_name, "content": content}
                    for file_name, content in TEST_DOCS
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\ndetailed output written to: {output_path}")

    success = bool(comparison["economy_domain_hits"]) and bool(
        comparison["changed_types"]
    )
    if success:
        print("\nRESULT: schema injection appears to affect extraction results.")
        return 0

    print("\nRESULT: no obvious extraction difference detected. Inspect schema_compare_output.json.")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
