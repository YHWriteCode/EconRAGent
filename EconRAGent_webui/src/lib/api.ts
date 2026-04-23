import type {
  ChatRequestPayload,
  ChatResponsePayload,
  ChatStreamEvent,
  DiscoverPagePayload,
  DiscoverSource,
  GraphEntityDetail,
  GraphFilters,
  GraphPathResponse,
  GraphPayload,
  GraphRelationDetail,
  ImportStatusPayload,
  SessionSummary,
  UploadRecord,
  WorkspaceImportPayload,
  WorkspaceSummary,
} from "../types";

export class ApiError extends Error {
  statusCode: number;

  constructor(message: string, statusCode: number) {
    super(message);
    this.name = "ApiError";
    this.statusCode = statusCode;
  }
}

function buildQuery(
  params: Record<string, string | number | boolean | undefined | null>,
): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === "") {
      continue;
    }
    search.set(key, String(value));
  }
  const serialized = search.toString();
  return serialized ? `?${serialized}` : "";
}

function extractErrorMessage(
  payload: unknown,
  fallback: string,
): string {
  if (typeof payload === "string" && payload.trim()) {
    return payload.trim();
  }
  if (payload && typeof payload === "object") {
    const typed = payload as Record<string, unknown>;
    const nested = typed.error;
    if (nested && typeof nested === "object") {
      const message = (nested as Record<string, unknown>).message;
      if (typeof message === "string" && message.trim()) {
        return message.trim();
      }
    }
    const detail = typed.detail;
    if (typeof detail === "string" && detail.trim()) {
      return detail.trim();
    }
    const message = typed.message;
    if (typeof message === "string" && message.trim()) {
      return message.trim();
    }
  }
  return fallback;
}

export async function fetchJson<T>(
  path: string,
  init?: RequestInit,
): Promise<T> {
  const response = await fetch(path, init);
  const contentType = response.headers.get("content-type") ?? "";
  const isJson = contentType.includes("application/json");
  const payload = isJson ? await response.json() : await response.text();
  if (!response.ok) {
    throw new ApiError(
      extractErrorMessage(payload, response.statusText || "Request failed"),
      response.status,
    );
  }
  if (!isJson) {
    throw new ApiError(
      "接口返回了非 JSON 响应，请确认 kg_agent 后端正在运行，并且开发服务器已代理 /agent。",
      response.status,
    );
  }
  return payload as T;
}

export async function listWorkspaces(): Promise<{ workspaces: WorkspaceSummary[] }> {
  return fetchJson("/agent/workspaces");
}

export async function createWorkspace(payload: {
  display_name: string;
  description?: string;
}): Promise<WorkspaceSummary> {
  return fetchJson("/agent/workspaces", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export async function updateWorkspace(
  workspaceId: string,
  payload: {
    display_name?: string;
    description?: string;
  },
): Promise<WorkspaceSummary> {
  return fetchJson(`/agent/workspaces/${encodeURIComponent(workspaceId)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export async function deleteWorkspace(workspaceId: string): Promise<{
  status: string;
  workspace_id: string;
}> {
  return fetchJson(`/agent/workspaces/${encodeURIComponent(workspaceId)}`, {
    method: "DELETE",
  });
}

export async function createWorkspaceImport(
  workspaceId: string,
  payload: WorkspaceImportPayload,
): Promise<{ status: string; workspace_id: string; track_id: string }> {
  return fetchJson(`/agent/workspaces/${encodeURIComponent(workspaceId)}/imports`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export async function getImportStatus(
  trackId: string,
  workspaceId?: string,
): Promise<ImportStatusPayload> {
  return fetchJson(
    `/agent/imports/${encodeURIComponent(trackId)}${buildQuery({
      workspace: workspaceId,
    })}`,
  );
}

export async function uploadFile(file: File): Promise<{
  upload_id: string;
  upload: UploadRecord;
}> {
  const body = new FormData();
  body.append("file", file);
  return fetchJson("/agent/uploads", {
    method: "POST",
    body,
  });
}

export async function getUpload(uploadId: string): Promise<UploadRecord> {
  return fetchJson(`/agent/uploads/${encodeURIComponent(uploadId)}`);
}

export async function listSessions(workspaceId?: string): Promise<{
  sessions: SessionSummary[];
}> {
  return fetchJson(
    `/agent/sessions${buildQuery({
      workspace: workspaceId,
    })}`,
  );
}

export async function getSessionMessages(sessionId: string): Promise<{
  session_id: string;
  messages: Array<Record<string, unknown>>;
}> {
  return fetchJson(`/agent/sessions/${encodeURIComponent(sessionId)}/messages`);
}

export async function deleteSession(sessionId: string): Promise<{
  status: string;
  session_id: string;
}> {
  return fetchJson(`/agent/sessions/${encodeURIComponent(sessionId)}`, {
    method: "DELETE",
  });
}

export async function getGraphData(filters: GraphFilters): Promise<GraphPayload> {
  const workspace = filters.workspace || "all";
  const useOverview =
    filters.label.trim() === "*" &&
    !filters.entityType.trim() &&
    !filters.timeFrom &&
    !filters.timeTo;

  if (useOverview) {
    return fetchJson(
      `/agent/graph/overview${buildQuery({
        workspace,
        max_nodes: filters.maxNodes,
      })}`,
    );
  }

  return fetchJson(
    `/agent/graph/subgraph${buildQuery({
      workspace,
      label: filters.label.trim() || "*",
      max_depth: filters.maxDepth,
      max_nodes: filters.maxNodes,
      entity_type: filters.entityType.trim() || undefined,
      time_from: filters.timeFrom || undefined,
      time_to: filters.timeTo || undefined,
    })}`,
  );
}

export async function searchGraphLabels(
  workspaceId: string,
  query: string,
): Promise<{ items: Array<{ workspace_id: string; label: string }> }> {
  return fetchJson(
    `/agent/graph/labels${buildQuery({
      workspace: workspaceId,
      q: query,
      limit: 10,
    })}`,
  );
}

export async function getGraphEntityDetail(
  workspaceId: string,
  entityId: string,
): Promise<GraphEntityDetail> {
  return fetchJson(
    `/agent/graph/entities/${encodeURIComponent(entityId)}${buildQuery({
      workspace: workspaceId,
    })}`,
  );
}

export async function getGraphRelationDetail(
  workspaceId: string,
  source: string,
  target: string,
): Promise<GraphRelationDetail> {
  return fetchJson(
    `/agent/graph/relations${buildQuery({
      workspace: workspaceId,
      source,
      target,
    })}`,
  );
}

export async function explainGraphPath(payload: {
  workspace: string;
  source: string;
  target: string;
}): Promise<GraphPathResponse> {
  return fetchJson("/agent/graph/path_explain", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export async function listDiscoverEvents(params: {
  workspace?: string;
  cursor?: string | null;
  limit?: number;
  category?: string;
}): Promise<DiscoverPagePayload> {
  return fetchJson(
    `/agent/discover/events${buildQuery({
      workspace: params.workspace,
      cursor: params.cursor,
      limit: params.limit ?? 12,
      category: params.category,
    })}`,
  );
}

export async function getDiscoverEvent(eventId: string) {
  return fetchJson(`/agent/discover/events/${encodeURIComponent(eventId)}`);
}

export async function listDiscoverSources(
  workspaceId?: string,
): Promise<{ sources: DiscoverSource[] }> {
  return fetchJson(
    `/agent/discover/sources${buildQuery({
      workspace: workspaceId,
    })}`,
  );
}

function parseSseEvent(chunk: string): ChatStreamEvent | null {
  const lines = chunk.split("\n");
  let data = "";
  for (const line of lines) {
    if (line.startsWith("data:")) {
      data += line.slice(5).trim();
    }
  }
  if (!data) {
    return null;
  }
  return JSON.parse(data) as ChatStreamEvent;
}

export async function streamChat(
  payload: ChatRequestPayload,
  handlers: {
    onEvent?: (event: ChatStreamEvent) => void;
  },
): Promise<ChatResponsePayload | null> {
  const response = await fetch("/agent/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      ...payload,
      stream: true,
    }),
  });

  if (!response.ok || !response.body) {
    let errorPayload: unknown = null;
    try {
      errorPayload = await response.json();
    } catch {
      errorPayload = null;
    }
    throw new ApiError(
      extractErrorMessage(errorPayload, response.statusText || "Streaming failed"),
      response.status,
    );
  }
  const contentType = response.headers.get("content-type") ?? "";
  if (!contentType.includes("text/event-stream")) {
    throw new ApiError(
      "对话接口返回了非流式响应，请确认 kg_agent 后端正在运行，并且开发服务器已代理 /agent。",
      response.status,
    );
  }

  const decoder = new TextDecoder();
  const reader = response.body.getReader();
  let buffer = "";
  let finalPayload: ChatResponsePayload | null = null;

  while (true) {
    const { done, value } = await reader.read();
    if (done) {
      break;
    }
    buffer += decoder.decode(value, { stream: true });
    let boundary = buffer.indexOf("\n\n");
    while (boundary >= 0) {
      const chunk = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      if (chunk.trim()) {
        const event = parseSseEvent(chunk);
        if (event) {
          handlers.onEvent?.(event);
          if (event.type === "done") {
            finalPayload = {
              answer: event.answer ?? "",
              route: event.route ?? {},
              tool_calls: event.tool_calls ?? [],
              path_explanation: event.path_explanation ?? null,
              metadata: event.metadata ?? {},
              streaming_supported: true,
            };
          }
          if (event.type === "error") {
            throw new ApiError(
              extractErrorMessage(event, "Streaming request failed"),
              500,
            );
          }
        }
      }
      boundary = buffer.indexOf("\n\n");
    }
  }

  return finalPayload;
}
