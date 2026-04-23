import type { GraphEdgePayload, GraphNodePayload } from "../types";

export const DEFAULT_NODE_COLOR = "#6f6255";

const TYPE_COLORS: Record<string, string> = {
  company: "#2f80ed",
  industry: "#00a878",
  metric: "#c05621",
  policy: "#8b5cf6",
  event: "#d9480f",
  asset: "#0f766e",
  institution: "#2563eb",
  country: "#64748b",
  organization: "#2f80ed",
  person: "#7c3aed",
  location: "#a16207",
  concept: "#b45309",
  unknown: DEFAULT_NODE_COLOR,
};

const TYPE_ALIASES: Record<string, string> = {
  org: "organization",
  organization: "organization",
  institution: "institution",
  institute: "institution",
  company: "company",
  corporation: "company",
  firm: "company",
  industry: "industry",
  sector: "industry",
  metric: "metric",
  indicator: "metric",
  data: "metric",
  policy: "policy",
  regulation: "policy",
  event: "event",
  asset: "asset",
  security: "asset",
  country: "country",
  nation: "country",
  location: "location",
  person: "person",
  concept: "concept",
  unknown: "unknown",
};

const EXTRA_COLORS = [
  "#1f7a8c",
  "#bf4342",
  "#6d597a",
  "#588157",
  "#bc6c25",
  "#3a5a40",
  "#99582a",
  "#3d405b",
];

export function normalizeEntityType(type: unknown): string {
  const normalized = String(type || "unknown").trim().toLowerCase();
  return TYPE_ALIASES[normalized] || normalized || "unknown";
}

export function resolveEntityTypeColor(type: unknown): string {
  const normalized = normalizeEntityType(type);
  if (TYPE_COLORS[normalized]) {
    return TYPE_COLORS[normalized];
  }
  let hash = 0;
  for (const char of normalized) {
    hash = (hash * 31 + char.charCodeAt(0)) % EXTRA_COLORS.length;
  }
  return EXTRA_COLORS[hash] || DEFAULT_NODE_COLOR;
}

export function resolveGraphNodeType(node: GraphNodePayload): string {
  return normalizeEntityType(node.properties.entity_type || node.properties.type);
}

export function resolveGraphNodeLabel(node: GraphNodePayload): string {
  return String(node.properties.entity_id || node.labels[0] || node.id);
}

export function resolveGraphNodeKey(node: GraphNodePayload): string {
  return `${node.workspace_id}::${node.id}`;
}

export function resolveGraphNodeSearchText(node: GraphNodePayload): string {
  return [
    resolveGraphNodeLabel(node),
    resolveGraphNodeType(node),
    node.id,
    node.workspace_id,
    ...node.labels,
  ]
    .join(" ")
    .toLowerCase();
}

export function resolveGraphEdgeLabel(edge: GraphEdgePayload): string {
  const value =
    edge.properties.keywords ||
    edge.properties.relation_type ||
    edge.type ||
    edge.properties.description ||
    "";
  return String(value).replace(/<SEP>/g, " / ");
}

export function resolveGraphEdgeWeight(edge: GraphEdgePayload): number {
  const value =
    edge.properties.weight ||
    edge.properties.confirmation_count ||
    edge.properties.strength ||
    edge.properties.score ||
    1;
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : 1;
}
