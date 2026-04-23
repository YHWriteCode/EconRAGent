export type QueryMode = "naive" | "local" | "global" | "hybrid" | "mix";
export type WebSearchMode = "auto" | "on" | "off";

export interface UploadRecord {
  upload_id: string;
  filename: string;
  stored_path: string;
  content_type: string;
  size_bytes: number;
  created_at: string;
  kind: string;
  extracted_text_path?: string | null;
  extracted_text_status?: string;
  extracted_text_error?: string | null;
  metadata?: Record<string, unknown>;
}

export interface ChatMessage {
  clientId: string;
  role: "user" | "assistant";
  content: string;
  timestamp: string;
  metadata: Record<string, unknown>;
  session_id?: string;
  user_id?: string | null;
}

export interface SessionSummary {
  session_id: string;
  user_id?: string | null;
  workspace?: string | null;
  title: string;
  created_at: string;
  last_message_at: string;
  message_count: number;
  last_message_preview: string;
}

export interface WorkspaceSummary {
  workspace_id: string;
  display_name: string;
  description?: string | null;
  created_at: string;
  updated_at: string;
  document_count: number;
  node_count: number;
  source_count: number;
  last_updated_at: string;
  archived: boolean;
}

export interface GraphNodePayload {
  id: string;
  labels: string[];
  properties: Record<string, unknown>;
  workspace_id: string;
}

export interface GraphEdgePayload {
  id: string;
  type?: string | null;
  source: string;
  target: string;
  properties: Record<string, unknown>;
  workspace_id: string;
}

export interface GraphSummary {
  node_count: number;
  edge_count: number;
  entity_type_counts: Record<string, number>;
  is_truncated: boolean;
}

export interface GraphPayload {
  workspace: string;
  nodes: GraphNodePayload[];
  edges: GraphEdgePayload[];
  summary: GraphSummary;
  is_truncated: boolean;
}

export interface GraphEntityDetail {
  workspace: string;
  entity_id: string;
  node: Record<string, unknown>;
  neighbors: Array<{
    entity_id: string;
    node: Record<string, unknown>;
  }>;
  relations: Array<{
    source: string;
    target: string;
    edge: Record<string, unknown>;
  }>;
}

export interface GraphRelationDetail {
  workspace: string;
  source: string;
  target: string;
  edge: Record<string, unknown>;
  source_node: Record<string, unknown> | null;
  target_node: Record<string, unknown> | null;
}

export interface GraphPathResponse {
  workspace: string;
  source: string;
  target: string;
  paths: Array<{
    path_text: string;
    nodes: Array<Record<string, unknown>>;
    edges: Array<Record<string, unknown>>;
  }>;
  path_explanation: {
    final_explanation?: string;
    uncertainty?: string | null;
  } | null;
}

export interface DiscoverSource {
  source_id: string;
  name: string;
  workspace?: string | null;
  category?: string | null;
  urls: string[];
}

export interface DiscoverSourceEntry {
  url: string;
  domain?: string | null;
  label: string;
  favicon_url?: string | null;
}

export interface DiscoverEvent {
  event_id: string;
  workspace?: string | null;
  source_id: string;
  cluster_id: string;
  category?: string | null;
  headline: string;
  summary: string;
  published_at?: string | null;
  updated_at?: string | null;
  sort_time?: string | null;
  source_count: number;
  sources: DiscoverSourceEntry[];
}

export interface DiscoverPagePayload {
  items: DiscoverEvent[];
  next_cursor?: string | null;
}

export interface ImportStatusPayload {
  track_id: string;
  workspace_id: string;
  document_count: number;
  status_counts: Record<string, number>;
  documents: Array<Record<string, unknown>>;
}

export interface GraphFilters {
  workspace: string;
  label: string;
  maxDepth: number;
  maxNodes: number;
  entityType: string;
  timeFrom: string;
  timeTo: string;
}

export interface ChatRequestPayload {
  query: string;
  session_id: string;
  workspace?: string;
  query_mode?: QueryMode;
  force_web_search?: boolean;
  attachment_ids?: string[];
}

export interface ChatResponsePayload {
  answer: string;
  route: Record<string, unknown>;
  tool_calls: Array<Record<string, unknown>>;
  path_explanation?: Record<string, unknown> | null;
  metadata: Record<string, unknown>;
  streaming_supported: boolean;
}

export interface ChatStreamEvent {
  type: string;
  content?: string;
  metadata?: Record<string, unknown>;
  answer?: string;
  route?: Record<string, unknown>;
  tool_calls?: Array<Record<string, unknown>>;
  tool_call?: Record<string, unknown>;
  skill_call?: Record<string, unknown>;
  path_explanation?: Record<string, unknown> | null;
}

export type WorkspaceImportPayload =
  | {
      kind: "text";
      text: string;
      source?: string;
    }
  | {
      kind: "url";
      url: string;
      source?: string;
    }
  | {
      kind: "upload";
      upload_id: string;
      source?: string;
    };
