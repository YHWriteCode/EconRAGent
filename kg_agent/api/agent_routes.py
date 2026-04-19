from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

from kg_agent.crawler.source_registry import MonitoredSource


def _error_code_for_status(status_code: int) -> str:
    if status_code == 422:
        return "validation_error"
    if status_code == 400:
        return "bad_request"
    if status_code == 404:
        return "not_found"
    if status_code == 503:
        return "service_unavailable"
    return "internal_error" if status_code >= 500 else "bad_request"


def _default_error_message(code: str) -> str:
    return {
        "validation_error": "Request validation failed.",
        "bad_request": "Bad request.",
        "not_found": "Resource not found.",
        "service_unavailable": "Service unavailable.",
        "internal_error": "Internal server error.",
        "stream_error": "Streaming request failed.",
    }.get(code, "Request failed.")


def _extract_error_message(detail: Any, *, fallback: str) -> str:
    if isinstance(detail, str):
        normalized = detail.strip()
        if normalized:
            return normalized
    if isinstance(detail, dict):
        value = detail.get("message")
        if isinstance(value, str):
            normalized = value.strip()
            if normalized:
                return normalized
    return fallback


def build_error_envelope(
    status_code: int,
    *,
    detail: Any = None,
    code: str | None = None,
    details: list[Any] | None = None,
    expose_internal_errors: bool = False,
    public_message: str | None = None,
) -> dict[str, Any]:
    resolved_code = code or _error_code_for_status(status_code)
    default_message = public_message or _default_error_message(resolved_code)
    if resolved_code in {"internal_error", "stream_error"} and not expose_internal_errors:
        message = default_message
    else:
        message = _extract_error_message(detail, fallback=default_message)
    payload: dict[str, Any] = {
        "error": {
            "code": resolved_code,
            "message": message,
            "status_code": status_code,
        }
    }
    if details is not None:
        payload["error"]["details"] = details
    return payload


def _format_sse_event(payload: dict[str, Any]) -> str:
    event_type = str(payload.get("type", "message")).strip() or "message"
    serialized = json.dumps(payload, ensure_ascii=False)
    return f"event: {event_type}\ndata: {serialized}\n\n"


def _build_stream_error_payload(
    exc: Exception,
    *,
    expose_internal_errors: bool,
) -> dict[str, Any]:
    return {
        "type": "error",
        **build_error_envelope(
            500,
            code="stream_error",
            detail=str(exc),
            expose_internal_errors=expose_internal_errors,
            public_message="Streaming request failed.",
        ),
    }


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
    kind: str = "native"
    executor: str = "tool_registry"


class ToolsResponse(BaseModel):
    tools: list[ToolInfo]


class SkillInfo(BaseModel):
    name: str
    description: str
    path: str
    tags: list[str]


class SkillFileInfo(BaseModel):
    path: str
    kind: str
    size_bytes: int


class SkillsResponse(BaseModel):
    skills: list[SkillInfo]


class SkillDetailResponse(BaseModel):
    skill: SkillInfo
    skill_md: str


class SkillFileResponse(BaseModel):
    skill: SkillInfo
    path: str
    kind: str
    content: str


class CapabilityInvokeRequest(BaseModel):
    session_id: str = Field(min_length=1)
    query: str = ""
    user_id: str | None = None
    workspace: str | None = None
    domain_schema: str | dict[str, Any] | None = None
    use_memory: bool = False
    args: dict[str, Any] = Field(default_factory=dict)

    @field_validator("query", mode="after")
    @classmethod
    def strip_optional_query(cls, value: str) -> str:
        return value.strip()


class CapabilityInvokeResult(BaseModel):
    tool: str
    success: bool
    optional: bool = False
    summary: str | None = None
    error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    data: Any = None


class CapabilityInvokeResponse(BaseModel):
    capability: ToolInfo
    result: CapabilityInvokeResult
    metadata: dict[str, Any] = Field(default_factory=dict)


class SkillInvokeRequest(BaseModel):
    session_id: str = Field(min_length=1)
    goal: str = Field(min_length=1)
    query: str = ""
    user_id: str | None = None
    workspace: str | None = None
    constraints: dict[str, Any] = Field(default_factory=dict)

    @field_validator("goal", mode="after")
    @classmethod
    def strip_goal(cls, value: str) -> str:
        return value.strip()

    @field_validator("query", mode="after")
    @classmethod
    def strip_skill_query(cls, value: str) -> str:
        return value.strip()


class SkillInvokeResult(BaseModel):
    tool: str
    success: bool
    optional: bool = False
    summary: str | None = None
    error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    data: Any = None


class SkillInvokeResponse(BaseModel):
    skill: SkillInfo
    result: SkillInvokeResult
    metadata: dict[str, Any] = Field(default_factory=dict)


class SkillRunStatusResponse(BaseModel):
    run_id: str
    skill_name: str | None = None
    run_status: str
    status: str
    success: bool = False
    command: str | None = None
    shell_mode: str | None = None
    runtime_target: dict[str, Any] = Field(default_factory=dict)
    workspace: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    exit_code: int | None = None
    failure_reason: str | None = None
    summary: str | None = None
    command_plan: dict[str, Any] = Field(default_factory=dict)
    runtime: dict[str, Any] = Field(default_factory=dict)
    execution_mode: str | None = None
    preflight: dict[str, Any] = Field(default_factory=dict)
    repair_attempted: bool = False
    repair_succeeded: bool = False
    repaired_from_run_id: str | None = None
    repair_attempt_count: int = 0
    repair_attempt_limit: int = 0
    repair_history: list[dict[str, Any]] = Field(default_factory=list)
    bootstrap_attempted: bool = False
    bootstrap_succeeded: bool = False
    bootstrap_attempt_count: int = 0
    bootstrap_attempt_limit: int = 0
    bootstrap_history: list[dict[str, Any]] = Field(default_factory=list)
    cancel_requested: bool = False


class SkillRunLogsResponse(BaseModel):
    run_id: str
    skill_name: str | None = None
    run_status: str | None = None
    status: str | None = None
    success: bool = False
    command: str | None = None
    shell_mode: str | None = None
    runtime_target: dict[str, Any] = Field(default_factory=dict)
    started_at: str | None = None
    finished_at: str | None = None
    failure_reason: str | None = None
    runtime: dict[str, Any] = Field(default_factory=dict)
    preflight: dict[str, Any] = Field(default_factory=dict)
    repair_attempted: bool = False
    repair_succeeded: bool = False
    repaired_from_run_id: str | None = None
    repair_attempt_count: int = 0
    repair_attempt_limit: int = 0
    repair_history: list[dict[str, Any]] = Field(default_factory=list)
    bootstrap_attempted: bool = False
    bootstrap_succeeded: bool = False
    bootstrap_attempt_count: int = 0
    bootstrap_attempt_limit: int = 0
    bootstrap_history: list[dict[str, Any]] = Field(default_factory=list)
    cancel_requested: bool = False
    stdout: str = ""
    stderr: str = ""
    summary: str | None = None


class SkillArtifactInfo(BaseModel):
    path: str
    size_bytes: int


class SkillRunArtifactsResponse(BaseModel):
    run_id: str
    skill_name: str | None = None
    run_status: str | None = None
    status: str | None = None
    success: bool = False
    shell_mode: str | None = None
    runtime_target: dict[str, Any] = Field(default_factory=dict)
    workspace: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    failure_reason: str | None = None
    runtime: dict[str, Any] = Field(default_factory=dict)
    preflight: dict[str, Any] = Field(default_factory=dict)
    repair_attempted: bool = False
    repair_succeeded: bool = False
    repaired_from_run_id: str | None = None
    repair_attempt_count: int = 0
    repair_attempt_limit: int = 0
    repair_history: list[dict[str, Any]] = Field(default_factory=list)
    bootstrap_attempted: bool = False
    bootstrap_succeeded: bool = False
    bootstrap_attempt_count: int = 0
    bootstrap_attempt_limit: int = 0
    bootstrap_history: list[dict[str, Any]] = Field(default_factory=list)
    cancel_requested: bool = False
    artifacts: list[SkillArtifactInfo] = Field(default_factory=list)
    summary: str | None = None


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


class FeedFilterConfigModel(BaseModel):
    include_patterns: list[str] = Field(default_factory=list)
    exclude_patterns: list[str] = Field(default_factory=list)
    include_authors: list[str] = Field(default_factory=list)
    exclude_authors: list[str] = Field(default_factory=list)
    include_categories: list[str] = Field(default_factory=list)
    exclude_categories: list[str] = Field(default_factory=list)
    allowed_domains: list[str] = Field(default_factory=list)
    blocked_domains: list[str] = Field(default_factory=list)
    max_age_days: float = Field(default=0.0, ge=0.0)


class FeedRetentionConfigModel(BaseModel):
    mode: str = "keep_all"
    max_items: int = Field(default=0, ge=0)
    max_age_days: float = Field(default=0.0, ge=0.0)


class FeedPriorityConfigModel(BaseModel):
    mode: str = "auto"
    priority_patterns: list[str] = Field(default_factory=list)
    preferred_domains: list[str] = Field(default_factory=list)
    preferred_authors: list[str] = Field(default_factory=list)
    preferred_categories: list[str] = Field(default_factory=list)


class FeedDedupConfigModel(BaseModel):
    mode: str = "auto"
    signature_token_limit: int = Field(default=120, ge=8)


class ContentLifecycleConfigModel(BaseModel):
    content_class: str = "auto"
    update_mode: str = "auto"
    ttl_days: float = Field(default=0.0, ge=0.0)
    delete_expired: bool = False
    event_cluster_mode: str = "auto"
    event_cluster_window_days: float = Field(default=3.0, ge=0.0)
    event_cluster_min_similarity: float = Field(default=0.72, ge=0.0, le=1.0)


class SourceRequest(BaseModel):
    source_id: str | None = None
    name: str = Field(min_length=1)
    urls: list[str] = Field(..., min_length=1)
    category: str = "general"
    interval_seconds: int = Field(default=3600, ge=1)
    max_pages: int = Field(default=3, ge=1)
    enabled: bool = True
    workspace: str | None = None
    source_type: str = "auto"
    schedule_mode: str = "auto"
    feed_filter: FeedFilterConfigModel = Field(default_factory=FeedFilterConfigModel)
    feed_retention: FeedRetentionConfigModel = Field(
        default_factory=FeedRetentionConfigModel
    )
    feed_priority: FeedPriorityConfigModel = Field(
        default_factory=FeedPriorityConfigModel
    )
    feed_dedup: FeedDedupConfigModel = Field(
        default_factory=FeedDedupConfigModel
    )
    content_lifecycle: ContentLifecycleConfigModel = Field(
        default_factory=ContentLifecycleConfigModel
    )

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
    source_type: str = "auto"
    schedule_mode: str = "auto"
    feed_filter: FeedFilterConfigModel = Field(default_factory=FeedFilterConfigModel)
    feed_retention: FeedRetentionConfigModel = Field(
        default_factory=FeedRetentionConfigModel
    )
    feed_priority: FeedPriorityConfigModel = Field(
        default_factory=FeedPriorityConfigModel
    )
    feed_dedup: FeedDedupConfigModel = Field(
        default_factory=FeedDedupConfigModel
    )
    content_lifecycle: ContentLifecycleConfigModel = Field(
        default_factory=ContentLifecycleConfigModel
    )
    resolved_source_type: str
    resolved_schedule_mode: str
    resolved_feed_priority_mode: str
    resolved_feed_dedup_mode: str
    resolved_content_class: str
    resolved_update_mode: str
    resolved_event_cluster_mode: str
    last_crawled_at: str | None = None
    last_status: str
    consecutive_failures: int = 0
    consecutive_no_change: int = 0
    tracked_item_count: int = 0
    active_doc_count: int = 0
    expired_doc_count: int = 0
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
    source_type: str = "auto"
    schedule_mode: str = "auto"
    feed_filter: FeedFilterConfigModel = Field(default_factory=FeedFilterConfigModel)
    feed_retention: FeedRetentionConfigModel = Field(
        default_factory=FeedRetentionConfigModel
    )
    feed_priority: FeedPriorityConfigModel = Field(
        default_factory=FeedPriorityConfigModel
    )
    feed_dedup: FeedDedupConfigModel = Field(
        default_factory=FeedDedupConfigModel
    )
    content_lifecycle: ContentLifecycleConfigModel = Field(
        default_factory=ContentLifecycleConfigModel
    )
    resolved_source_type: str
    resolved_schedule_mode: str
    resolved_feed_priority_mode: str
    resolved_feed_dedup_mode: str
    resolved_content_class: str
    resolved_update_mode: str
    resolved_event_cluster_mode: str


class SourceMutationResponse(BaseModel):
    status: str
    source_id: str


class SchedulerTriggerResponse(BaseModel):
    status: str
    source_id: str
    requested_count: int | None = None
    success_count: int | None = None
    ingested_count: int | None = None
    feed_discovered_count: int | None = None
    feed_filtered_count: int | None = None
    feed_deduplicated_count: int | None = None
    superseded_count: int | None = None
    expired_doc_count: int | None = None
    deleted_doc_count: int | None = None
    tracked_item_count: int | None = None
    summary: str = ""


def create_agent_routes(
    agent_core,
    scheduler=None,
    *,
    expose_internal_errors: bool = False,
):
    router = APIRouter(tags=["agent"])

    async def _sse_stream(request: ChatRequest):
        try:
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
                yield _format_sse_event(event)
        except Exception as exc:
            yield _format_sse_event(
                _build_stream_error_payload(
                    exc,
                    expose_internal_errors=expose_internal_errors,
                )
            )

    @router.post("/agent/chat", response_model=ChatResponse)
    async def agent_chat(request: ChatRequest):
        if request.stream:
            return StreamingResponse(
                _sse_stream(request),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                },
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

    @router.post("/agent/ingest", response_model=IngestResponse)
    async def agent_ingest(request: IngestRequest):
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

    @router.get("/agent/tools", response_model=ToolsResponse)
    async def list_agent_tools():
        return {"tools": agent_core.list_tools()}

    @router.get("/agent/skills", response_model=SkillsResponse)
    async def list_agent_skills():
        return {"skills": agent_core.list_skills()}

    @router.get("/agent/skills/{skill_name}", response_model=SkillDetailResponse)
    async def read_agent_skill(skill_name: str):
        try:
            return agent_core.read_skill(skill_name)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get(
        "/agent/skills/{skill_name}/files/{relative_path:path}",
        response_model=SkillFileResponse,
    )
    async def read_agent_skill_file(skill_name: str, relative_path: str):
        try:
            return agent_core.read_skill_file(skill_name, relative_path)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post(
        "/agent/skills/{skill_name}/invoke",
        response_model=SkillInvokeResponse,
    )
    async def invoke_skill(skill_name: str, request: SkillInvokeRequest):
        try:
            response = await agent_core.invoke_skill(
                skill_name=skill_name,
                session_id=request.session_id,
                goal=request.goal,
                query=request.query,
                user_id=request.user_id,
                workspace=request.workspace,
                constraints=request.constraints,
            )
            return response.to_dict()
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get(
        "/agent/skill-runs/{run_id}",
        response_model=SkillRunStatusResponse,
    )
    async def get_skill_run_status(run_id: str):
        try:
            return await agent_core.get_skill_run_status(run_id=run_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @router.post(
        "/agent/skill-runs/{run_id}/cancel",
        response_model=SkillRunStatusResponse,
    )
    async def cancel_skill_run(run_id: str):
        try:
            return await agent_core.cancel_skill_run(run_id=run_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @router.get(
        "/agent/skill-runs/{run_id}/logs",
        response_model=SkillRunLogsResponse,
    )
    async def get_skill_run_logs(run_id: str):
        try:
            return await agent_core.get_skill_run_logs(run_id=run_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @router.get(
        "/agent/skill-runs/{run_id}/artifacts",
        response_model=SkillRunArtifactsResponse,
    )
    async def get_skill_run_artifacts(run_id: str):
        try:
            return await agent_core.get_skill_run_artifacts(run_id=run_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @router.post(
        "/agent/capabilities/{capability_name}/invoke",
        response_model=CapabilityInvokeResponse,
    )
    async def invoke_capability(capability_name: str, request: CapabilityInvokeRequest):
        try:
            response = await agent_core.invoke_capability(
                capability_name=capability_name,
                session_id=request.session_id,
                query=request.query,
                user_id=request.user_id,
                workspace=request.workspace,
                domain_schema=request.domain_schema,
                use_memory=request.use_memory,
                args=request.args,
            )
            return response.to_dict()
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/agent/route_preview", response_model=RoutePreviewResponse)
    async def route_preview(request: RoutePreviewRequest):
        route = await agent_core.preview_route(
            query=request.query,
            session_id=request.session_id,
            user_id=request.user_id,
            use_memory=request.use_memory,
        )
        return {"route": asdict(route)}

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
        utility_available = scheduler.utility_llm_available()
        return {
            "sources": [
                source.to_public_dict(utility_available=utility_available)
                for source in sources
            ]
        }

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
                source_type=request.source_type,
                schedule_mode=request.schedule_mode,
                feed_filter=request.feed_filter.model_dump(),
                feed_retention=request.feed_retention.model_dump(),
                feed_priority=request.feed_priority.model_dump(),
                feed_dedup=request.feed_dedup.model_dump(),
                content_lifecycle=request.content_lifecycle.model_dump(),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        stored = await scheduler.add_source(source)
        return stored.to_public_dict(
            utility_available=scheduler.utility_llm_available()
        )

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
