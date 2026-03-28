from __future__ import annotations

import asyncio
import importlib.util
import sys
import time
from pathlib import Path


DOCS = [
    (
        "macro_01.txt",
        "In the second half of 2025, domestic macro policy continues to emphasize steady growth and "
        "high-quality development. A manufacturing equipment company increases investment and benefits "
        "from tax incentives, green credit, and local industrial support policies.",
    ),
    (
        "energy_02.txt",
        "Battery materials producers improved gross margins after lithium carbonate prices retreated, "
        "while local governments introduced green manufacturing incentives and guided banks to expand "
        "long-term credit. The policy support encouraged one cathode materials company to add new "
        "production lines and accelerate capital expenditure.",
    ),
    (
        "semicon_03.txt",
        "A semiconductor equipment supplier gained new orders as downstream capital expenditure "
        "recovered. The company expanded research investment to strengthen its technology moat and "
        "benefited from import substitution demand.",
    ),
    (
        "retail_04.txt",
        "A consumer retail chain improved same-store sales during the holiday season, but inventory "
        "adjustments and discount campaigns continued to pressure gross margin. Management expects "
        "profitability to recover gradually next quarter.",
    ),
    (
        "cloud_05.txt",
        "A cloud infrastructure provider raised revenue guidance as enterprise demand for AI computing "
        "power expanded. The firm also controlled operating expenses to improve free cash flow and "
        "increase data center utilization.",
    ),
    (
        "bank_06.txt",
        "A regional bank expanded medium and long-term lending to small and medium-sized enterprises "
        "after local authorities encouraged credit support for employment stabilization and industrial "
        "investment. Net interest margin remained under pressure while asset quality stayed stable.",
    ),
]


def _load_e2e_module():
    path = Path(__file__).resolve().with_name("test_pipeline_e2e.py")
    spec = importlib.util.spec_from_file_location("e2e_mod", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


async def main() -> None:
    mod = _load_e2e_module()
    mod.load_env_files()
    mod.prepare_env_aliases()
    mod.reset_llm_log()

    from lightrag_fork import LightRAG

    workspace = f"bulk_six_{int(time.time())}"
    rag = LightRAG(
        working_dir=str(mod.RAG_STORAGE_DIR),
        workspace=workspace,
        kv_storage="RedisKVStorage",
        vector_storage="QdrantVectorDBStorage",
        graph_storage="Neo4JStorage",
        doc_status_storage="MongoDocStatusStorage",
        llm_model_func=mod._build_openai_compatible_llm_func(
            llm_base_url=mod.os.getenv("OPENAI_API_BASE"),
            llm_api_key=mod.os.getenv("OPENAI_API_KEY"),
            llm_model_name=mod.os.getenv("LLM_MODEL_NAME", "gpt-4o-mini"),
            llm_timeout=mod._read_int_env("LLM_TIMEOUT"),
        ),
        llm_model_name=mod.os.getenv("LLM_MODEL_NAME", "gpt-4o-mini"),
        llm_model_kwargs={
            "base_url": mod.os.getenv("OPENAI_API_BASE"),
            "api_key": mod.os.getenv("OPENAI_API_KEY"),
        },
        embedding_func=mod._build_embedding_func(
            mod.os.getenv("EMBEDDING_BINDING_HOST") or mod.os.getenv("OPENAI_API_BASE"),
            mod.os.getenv("EMBEDDING_BINDING_API_KEY") or mod.os.getenv("OPENAI_API_KEY"),
            mod.os.getenv("EMBEDDING_MODEL"),
            mod._read_int_env("EMBEDDING_DIM"),
        ),
    )

    await rag.initialize_storages()
    try:
        results = []
        for index, (file_name, text) in enumerate(DOCS, start=1):
            doc_id = f"doc-{index}-{int(time.time())}"
            try:
                await rag.ainsert(text, ids=[doc_id], file_paths=[file_name])
                doc = await mod._wait_doc_processed(rag, doc_id, timeout_s=180)
                results.append(
                    {
                        "file": file_name,
                        "doc_id": doc_id,
                        "status": doc.get("status"),
                    }
                )
            except Exception as e:
                results.append(
                    {
                        "file": file_name,
                        "doc_id": doc_id,
                        "status": f"failed: {e}",
                    }
                )

        nodes = await rag.chunk_entity_relation_graph.get_all_nodes()
        print(
            {
                "workspace": workspace,
                "node_count": len(nodes),
                "results": results,
                "llm_log": str(mod.LLM_LOG_PATH),
            }
        )
    finally:
        await mod._cleanup_workspace_data(rag)
        await rag.finalize_storages()


if __name__ == "__main__":
    asyncio.run(main())
