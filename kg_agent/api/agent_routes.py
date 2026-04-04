from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

from kg_agent.crawler.source_registry import MonitoredSource


class ChatRequest(BaseModel):
    query: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    user_id: str | None = None
    workspace: str | None = None
    domain_schema: str | dict[str, Any] | None = None
    max_iterations: int | None = Field(default=None, ge=1, le=8)
    use_memory: bool = True
    debug: bool = False
    stream: bool = False

    @field_validator("query", mode="after")
    @classmethod
    def strip_query(cls, value: str) -> str:
        return value.strip()


class ChatResponse(BaseModel):
    answer: str
    route: dict[str, Any]
    tool_calls: list[dict[str, Any]]
    path_explanation: dict[str, Any] | None = None
    metadata: dict[str, Any]
    streaming_supported: bool = False


class ToolInfo(BaseModel):
    name: str
    description: str
    input_schema: dict[str, Any]
    enabled: bool
    tags: list[str]


class ToolsResponse(BaseModel):
    tools: list[ToolInfo]


class RoutePreviewRequest(BaseModel):
    query: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    user_id: str | None = None
    use_memory: bool = True


class RoutePreviewResponse(BaseModel):
    route: dict[str, Any]


class IngestRequest(BaseModel):
    content: str | list[str] = Field(
        ..., description="Text or markdown content to ingest into the knowledge graph"
    )
    source: str | list[str] | None = Field(
        default=None,
        description="Provenance label (URL, file path, or descriptive tag)",
    )
    workspace: str | None = Field(
        default=None,
        description="Target workspace (uses default if omitted)",
    )

    @field_validator("content", mode="after")
    @classmethod
    def validate_content(cls, value: str | list[str]) -> str | list[str]:
        if isinstance(value, str):
            if not value.strip():
                raise ValueError("content must be non-empty")
            return value
        filtered = [v for v in value if isinstance(v, str) and v.strip()]
        if not filtered:
            raise ValueError("content list must contain at least one non-empty string")
        return filtered


class IngestResponse(BaseModel):
    status: str
    track_id: str | None = None
    document_count: int = 0
    source: str | list[str] | None = None
    message: str = ""


class SourceRequest(BaseModel):
    source_id: str | None = None
    name: str = Field(min_length=1)
    urls: list[str] = Field(..., min_length=1)
    category: str = "general"
    interval_seconds: int = Field(default=3600, ge=1)
    max_pages: int = Field(default=3, ge=1)
    enabled: bool = True
    workspace: str | None = None

    @field_validator("name", mode="after")
    @classmethod
    def strip_name(cls, value: str) -> str:
        return value.strip()

    @field_validator("urls", mode="after")
    @classmethod
    def validate_urls(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value if isinstance(item, str) and item.strip()]
        if not normalized:
            raise ValueError("urls must contain at least one non-empty URL")
        return normalized


class SourceListResponse(BaseModel):
    sources: list["SourceResponse"]


class SchedulerSourceStatus(BaseModel):
    source_id: str
    name: str
    urls: list[str]
    category: str
    interval_seconds: int
    max_pages: int
    enabled: bool
    workspace: str | None = None
    last_crawled_at: str | None = None
    last_status: str
    consecutive_failures: int = 0
    total_ingested_count: int = 0
    last_error: str | None = None
    effective_interval_seconds: int
    next_poll_due_in_seconds: int | None = None


class SchedulerStatusResponse(BaseModel):
    configured: bool
    enabled: bool
    running: bool
    check_interval_seconds: int
    coordination_backend: str = "local"
    leader_election_enabled: bool = False
    leader_role: str = "disabled"
    loop_lease_key: str | None = None
    started_at: str | None = None
    last_tick_at: str | None = None
    last_error: str | None = None
    loop_iterations: int = 0
    sources_file: str | None = None
    state_file: str | None = None
    source_count: int = 0
    sources: list[SchedulerSourceStatus] = Field(default_factory=list)


class SourceResponse(BaseModel):
    source_id: str
    name: str
    urls: list[str]
    category: str
    interval_seconds: int
    max_pages: int
    enabled: bool
    workspace: str | None = None


class SourceMutationResponse(BaseModel):
    status: str
    source_id: str


class SchedulerTriggerResponse(BaseModel):
    status: str
    source_id: str
    requested_count: int | None = None
    success_count: int | None = None
    ingested_count: int | None = None
    summary: str = ""


def create_agent_routes(agent_core, scheduler=None):
    router = APIRouter(tags=["agent"])

    async def _sse_stream(request: ChatRequest):
        async for event in agent_core.chat_stream(
            query=request.query,
            session_id=request.session_id,
            user_id=request.user_id,
            workspace=request.workspace,
            domain_schema=request.domain_schema,
            max_iterations=request.max_iterations,
            use_memory=request.use_memory,
            debug=request.debug,
        ):
            payload = json.dumps(event, ensure_ascii=False)
            yield f"data: {payload}\n\n"

    @router.post("/agent/chat", response_model=ChatResponse)
    async def agent_chat(request: ChatRequest):
        try:
            if request.stream:
                return StreamingResponse(
                    _sse_stream(request),
                    media_type="text/event-stream",
                )
            response = await agent_core.chat(
                query=request.query,
                session_id=request.session_id,
                user_id=request.user_id,
                workspace=request.workspace,
                domain_schema=request.domain_schema,
                max_iterations=request.max_iterations,
                use_memory=request.use_memory,
                debug=request.debug,
                stream=request.stream,
            )
            return response.to_dict()
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @router.post("/agent/ingest", response_model=IngestResponse)
    async def agent_ingest(request: IngestRequest):
        try:
            rag = await agent_core._resolve_rag(request.workspace)
            docs = (
                [request.content]
                if isinstance(request.content, str)
                else request.content
            )
            track_id = await rag.ainsert(
                input=docs if len(docs) > 1 else docs[0],
                file_paths=request.source,
            )
            return IngestResponse(
                status="accepted",
                track_id=track_id,
                document_count=len(docs),
                source=request.source,
                message=f"Accepted {len(docs)} document(s) for ingestion",
            )
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @router.get("/agent/tools", response_model=ToolsResponse)
    async def list_agent_tools():
        return {"tools": agent_core.list_tools()}

    @router.post("/agent/route_preview", response_model=RoutePreviewResponse)
    async def route_preview(request: RoutePreviewRequest):
        try:
            route = await agent_core.preview_route(
                query=request.query,
                session_id=request.session_id,
                user_id=request.user_id,
                use_memory=request.use_memory,
            )
            from dataclasses import asdict

            return {"route": asdict(route)}
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @router.get("/agent/scheduler/status", response_model=SchedulerStatusResponse)
    async def scheduler_status():
        if scheduler is None:
            return SchedulerStatusResponse(
                configured=False,
                enabled=False,
                running=False,
                check_interval_seconds=0,
                coordination_backend="local",
                leader_election_enabled=False,
                leader_role="disabled",
                loop_lease_key=None,
                source_count=0,
                sources=[],
            )
        return await scheduler.get_status()

    @router.get("/agent/sources", response_model=SourceListResponse)
    async def list_sources():
        if scheduler is None:
            raise HTTPException(status_code=503, detail="Scheduler is not configured")
        sources = await scheduler.list_sources()
        return {"sources": [source.to_dict() for source in sources]}

    @router.post("/agent/sources", response_model=SourceResponse)
    async def add_source(request: SourceRequest):
        if scheduler is None:
            raise HTTPException(status_code=503, detail="Scheduler is not configured")
        try:
            source = MonitoredSource(
                source_id=request.source_id or "",
                name=request.name,
                urls=request.urls,
                category=request.category,
                interval_seconds=request.interval_seconds,
                max_pages=request.max_pages,
                enabled=request.enabled,
                workspace=request.workspace,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        stored = await scheduler.add_source(source)
        return stored.to_dict()

    @router.delete("/agent/sources/{source_id}", response_model=SourceMutationResponse)
    async def delete_source(source_id: str):
        if scheduler is None:
            raise HTTPException(status_code=503, detail="Scheduler is not configured")
        removed = await scheduler.remove_source(source_id)
        if not removed:
            raise HTTPException(status_code=404, detail="Source not found")
        return {"status": "deleted", "source_id": source_id}

    @router.post(
        "/agent/sources/{source_id}/trigger",
        response_model=SchedulerTriggerResponse,
    )
    async def trigger_source(source_id: str):
        if scheduler is None:
            raise HTTPException(status_code=503, detail="Scheduler is not configured")
        result = await scheduler.trigger_now(source_id)
        if result.get("status") == "not_found":
            raise HTTPException(status_code=404, detail="Source not found")
        return result

    return router
