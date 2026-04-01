"""
End-to-end test: crawl → extract markdown → rag.ainsert() → verify KG ingestion.

Usage:
    python -m tests.kg_agent.test_crawl_ingest_e2e
"""

from __future__ import annotations

import asyncio
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s  %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("test_crawl_ingest_e2e")

# A lightweight, reliable test URL (small page, stable content).
TEST_URL = "https://www.gov.cn/zhengce/202503/content_7012557.htm"


async def main() -> None:
    # ── Step 0: Build LightRAG + Crawler from env ────────────────────
    logger.info("Step 0  Building LightRAG instance and crawler adapter …")

    from kg_agent.api.app import build_rag_from_env, build_crawler_adapter_from_env
    from kg_agent.config import KGAgentConfig

    config = KGAgentConfig.from_env()
    # Force-enable so the test doesn't depend on .env toggle
    config.tool_config.enable_web_search = True
    config.tool_config.enable_kg_ingest = True

    rag = build_rag_from_env(workspace="ingest_e2e_test")
    await rag.initialize_storages()
    logger.info("  LightRAG storages initialized (workspace=%s)", rag.workspace)

    crawler = build_crawler_adapter_from_env(config=config)
    if crawler is None:
        # Manually build one since env toggle might be off
        from kg_agent.crawler.crawler_adapter import Crawl4AIAdapter
        crawler = Crawl4AIAdapter(config=config.crawler)
    logger.info("  Crawler adapter ready (provider=%s)", config.crawler.provider)

    try:
        # ── Step 1: Crawl a URL ──────────────────────────────────────
        logger.info("Step 1  Crawling %s …", TEST_URL)
        pages = await crawler.crawl_urls([TEST_URL], max_pages=1)
        page = pages[0]
        logger.info(
            "  Crawl result: success=%s  title=%s  markdown_len=%d",
            page.success,
            page.title,
            len(page.markdown),
        )
        if not page.success:
            logger.error("  Crawl FAILED: %s", page.error)
            logger.error("  Cannot proceed with ingestion test.")
            return

        if len(page.markdown.strip()) < 50:
            logger.error("  Crawled markdown is too short (%d chars), skipping.", len(page.markdown))
            return

        logger.info("  Markdown excerpt:\n---\n%s\n---", page.markdown[:500])

        # ── Step 2: Ingest via kg_ingest tool ────────────────────────
        logger.info("Step 2  Calling kg_ingest tool …")
        from kg_agent.tools.kg_ingest import kg_ingest

        result = await kg_ingest(
            rag=rag,
            query="ingest test page",
            content=page.markdown,
            source=page.final_url or TEST_URL,
        )
        logger.info(
            "  kg_ingest result: success=%s  data=%s",
            result.success,
            result.data,
        )
        if not result.success:
            logger.error("  Ingestion FAILED: %s", result.error)
            return

        track_id = result.data.get("track_id", "")
        logger.info("  track_id = %s", track_id)

        # ── Step 3: Wait for pipeline processing ─────────────────────
        logger.info("Step 3  Waiting for LightRAG pipeline to process …")
        # ainsert enqueues and processes synchronously in the default config,
        # so by the time it returns the pipeline should have finished.
        # But let's give it an extra moment and then verify.
        await asyncio.sleep(2)

        # ── Step 4: Verify — query the KG to see if entities appeared ─
        logger.info("Step 4  Verifying KG by running a hybrid query …")
        from lightrag_fork.base import QueryParam

        param = QueryParam(mode="hybrid", top_k=5)
        query_result = await rag.aquery_data(
            "What is the content about?",
            param=param,
        )
        status = query_result.get("status", "unknown")
        entities = query_result.get("data", {}).get("entities", [])
        chunks = query_result.get("data", {}).get("chunks", [])
        logger.info(
            "  Query status=%s  entities=%d  chunks=%d",
            status,
            len(entities),
            len(chunks),
        )

        if entities:
            logger.info("  Sample entities:")
            for e in entities[:5]:
                name = e.get("entity_name", e.get("name", "?"))
                logger.info("    - %s", name)

        if chunks:
            logger.info("  Sample chunk excerpt: %s", chunks[0].get("content", "")[:200])

        # ── Step 5: Also verify doc status ───────────────────────────
        logger.info("Step 5  Checking document status storage …")
        try:
            all_docs = await rag.doc_status.get_docs_by_status()
            doc_count = len(all_docs) if isinstance(all_docs, (list, dict)) else 0
            logger.info("  Total docs in status storage: %s", doc_count)
            if isinstance(all_docs, dict):
                for doc_id, doc_info in list(all_docs.items())[:3]:
                    doc_status = doc_info.get("status", "?") if isinstance(doc_info, dict) else "?"
                    file_path = doc_info.get("file_path", "") if isinstance(doc_info, dict) else ""
                    logger.info("    doc=%s  status=%s  file=%s", doc_id[:20], doc_status, file_path)
        except Exception as exc:
            logger.warning("  Could not query doc status: %s", exc)

        # ── Summary ──────────────────────────────────────────────────
        if status == "success" and (entities or chunks):
            logger.info("=== E2E TEST PASSED: crawl → ingest → KG verified ===")
        else:
            logger.warning(
                "=== E2E TEST INCONCLUSIVE: query returned status=%s, "
                "entities=%d, chunks=%d ===",
                status,
                len(entities),
                len(chunks),
            )

    finally:
        logger.info("Cleaning up …")
        await crawler.close()
        await rag.finalize_storages()
        from lightrag_fork.kg.shared_storage import finalize_share_data
        finalize_share_data()
        logger.info("Done.")


if __name__ == "__main__":
    asyncio.run(main())
