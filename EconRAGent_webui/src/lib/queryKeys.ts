export const queryKeys = {
  workspaces: ["workspaces"] as const,
  sessions: (workspaceId: string, userId = "") =>
    ["sessions", workspaceId, userId] as const,
  sessionMessages: (sessionId: string) =>
    ["session-messages", sessionId] as const,
  graph: (scope: string) => ["graph", scope] as const,
  graphSchema: (workspaceId: string) => ["graph-schema", workspaceId] as const,
  graphLabels: (workspaceId: string, query: string) =>
    ["graph-labels", workspaceId, query] as const,
  discover: (workspaceId: string, category: string) =>
    ["discover", workspaceId, category] as const,
  importStatus: (trackId: string, workspaceId: string) =>
    ["import-status", trackId, workspaceId] as const,
} as const;
