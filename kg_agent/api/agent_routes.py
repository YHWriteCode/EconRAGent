from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator


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
    source: str | None = Field(
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
    source: str | None = None
    message: str = ""


def create_agent_routes(agent_core):
    router = APIRouter(tags=["agent"])

    @router.post("/agent/chat", response_model=ChatResponse)
    async def agent_chat(request: ChatRequest):
        try:
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

    return router
