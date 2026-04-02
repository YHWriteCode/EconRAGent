"""KG ingestion tool: insert text or markdown content into the knowledge graph.

Accepts content from any source (web crawl, PDF extraction, manual input, etc.)
and delegates to ``LightRAG.ainsert()`` for chunking, entity extraction, and
graph merging.
"""

from __future__ import annotations

import logging
from typing import Any

from kg_agent.tools.base import ToolResult

logger = logging.getLogger(__name__)


async def kg_ingest(
    *,
    rag,
    query: str,
    content: str | list[str] | None = None,
    source: str | list[str] | None = None,
    **_: Any,
) -> ToolResult:
    """Insert text content into the knowledge graph via ``rag.ainsert()``.

    Parameters
    ----------
    rag:
        A ``LightRAG`` instance (injected by the framework).
    query:
        The user query that triggered this tool (used for logging / metadata).
    content:
        The text or markdown to ingest.  May be a single string or a list of
        strings (multiple documents).  If *None* or empty, the tool returns a
        failure result.
    source:
        Optional provenance label persisted as ``file_paths`` in document
        status storage.  For web pages this would be the URL; for PDFs the
        file path; for manual input it can be any descriptive tag.
    """
    if not content:
        return ToolResult(
            tool_name="kg_ingest",
            success=False,
            data={"status": "empty_content", "summary": "No content provided for ingestion"},
            error="The 'content' parameter is required and must be non-empty",
        )

    docs: list[str]
    if isinstance(content, str):
        docs = [content]
    else:
        docs = [c for c in content if isinstance(c, str) and c.strip()]

    if not docs:
        return ToolResult(
            tool_name="kg_ingest",
            success=False,
            data={"status": "empty_content", "summary": "All provided documents were empty after filtering"},
            error="No valid document content after filtering",
        )

    try:
        track_id = await rag.ainsert(
            input=docs if len(docs) > 1 else docs[0],
            file_paths=source,
        )
    except Exception as exc:
        logger.exception("kg_ingest failed: %s", exc)
        return ToolResult(
            tool_name="kg_ingest",
            success=False,
            data={
                "status": "error",
                "summary": f"Ingestion failed: {exc}",
            },
            error=str(exc),
        )

    return ToolResult(
        tool_name="kg_ingest",
        success=True,
        data={
            "status": "accepted",
            "track_id": track_id,
            "document_count": len(docs),
            "source": source,
            "summary": (
                f"Accepted {len(docs)} document(s) for ingestion "
                f"(track_id={track_id})"
            ),
        },
        metadata={
            "track_id": track_id,
            "document_count": len(docs),
        },
    )
