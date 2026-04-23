import type {
  GraphEntityDetail,
  GraphPathResponse,
  GraphRelationDetail,
} from "../types";
import { resolveEntityTypeColor } from "../lib/graphVisual";

export type GraphDrawerState = {
  kind: "empty" | "entity" | "relation" | "path" | "error";
  payload?: unknown;
  title?: string;
};

interface GraphDetailsPanelProps {
  drawer: GraphDrawerState;
  nodeCount: number;
  edgeCount: number;
  entityTypeEntries: Array<[string, number]>;
  pathLoading?: boolean;
  onClose: () => void;
  onEntityTypeSelect: (entityType: string) => void;
  onEntitySelect: (workspaceId: string, entityId: string) => void;
  onRelationSelect: (workspaceId: string, source: string, target: string) => void;
  onPathExplain: (payload: { workspace: string; source: string; target: string }) => void;
  onFocusGraph: (query: string) => void;
}

const HIDDEN_KEYS = new Set(["embedding", "vector", "truncate"]);
const PRIORITY_KEYS = [
  "entity_type",
  "relation_type",
  "keywords",
  "description",
  "weight",
  "confirmation_count",
  "created_at",
  "last_confirmed_at",
  "source_id",
];

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function getString(record: Record<string, unknown>, keys: string[], fallback = "") {
  for (const key of keys) {
    const value = record[key];
    if (typeof value === "string" && value.trim()) {
      return value.trim();
    }
    if (typeof value === "number") {
      return String(value);
    }
  }
  return fallback;
}

function formatValue(value: unknown): string {
  if (value === null || value === undefined || value === "") {
    return "无";
  }
  if (typeof value === "string") {
    return value.replace(/<SEP>/g, ";\n");
  }
  if (typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  return JSON.stringify(value, null, 2);
}

function formatLabel(key: string): string {
  const labels: Record<string, string> = {
    entity_type: "实体类型",
    relation_type: "关系类型",
    keywords: "关键词",
    description: "说明",
    weight: "权重",
    confirmation_count: "确认次数",
    created_at: "创建时间",
    last_confirmed_at: "最近确认",
    source_id: "来源",
  };
  return labels[key] || key;
}

function visibleEntries(record: Record<string, unknown>, limit = 12) {
  const entries = Object.entries(record).filter(([key, value]) => {
    if (HIDDEN_KEYS.has(key)) {
      return false;
    }
    if (value === null || value === undefined || value === "") {
      return false;
    }
    return !Array.isArray(value) || value.length > 0;
  });
  entries.sort(([a], [b]) => {
    const left = PRIORITY_KEYS.indexOf(a);
    const right = PRIORITY_KEYS.indexOf(b);
    if (left === -1 && right === -1) {
      return a.localeCompare(b);
    }
    if (left === -1) {
      return 1;
    }
    if (right === -1) {
      return -1;
    }
    return left - right;
  });
  return entries.slice(0, limit);
}

function entityTitle(node: Record<string, unknown>, fallback: string) {
  return getString(node, ["entity_id", "name", "label", "id"], fallback);
}

function entityType(node: Record<string, unknown>) {
  return getString(node, ["entity_type", "type"], "unknown");
}

function relationTitle(edge: Record<string, unknown>) {
  return getString(edge, ["keywords", "relation_type", "description"], "关系");
}

function DetailHeader({
  title,
  subtitle,
  onClose,
}: {
  title: string;
  subtitle?: string;
  onClose: () => void;
}) {
  return (
    <div className="detail-header">
      <div>
        <span className="detail-kicker">{subtitle}</span>
        <h3>{title}</h3>
      </div>
      <button className="ghost-button" type="button" onClick={onClose}>
        关闭
      </button>
    </div>
  );
}

function KeyValueList({
  title,
  record,
  limit,
}: {
  title: string;
  record: Record<string, unknown>;
  limit?: number;
}) {
  const entries = visibleEntries(record, limit);
  if (!entries.length) {
    return null;
  }
  return (
    <div className="detail-block">
      <h4>{title}</h4>
      <dl className="detail-kv">
        {entries.map(([key, value]) => (
          <div key={key}>
            <dt>{formatLabel(key)}</dt>
            <dd>{formatValue(value)}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

function TypeBadge({ type }: { type: string }) {
  return (
    <span className="entity-type-badge">
      <span
        className="legend-dot"
        style={{ backgroundColor: resolveEntityTypeColor(type) }}
      />
      {type}
    </span>
  );
}

function EmptyDetails({
  nodeCount,
  edgeCount,
  entityTypeEntries,
  onEntityTypeSelect,
}: Pick<
  GraphDetailsPanelProps,
  "nodeCount" | "edgeCount" | "entityTypeEntries" | "onEntityTypeSelect"
>) {
  return (
    <div className="stack">
      <h3>概览与详情</h3>
      <div className="graph-sidebar-meta">
        <div className="stat-box">
          <span className="stat-label">节点</span>
          <span className="stat-value">{nodeCount}</span>
        </div>
        <div className="stat-box">
          <span className="stat-label">边</span>
          <span className="stat-value">{edgeCount}</span>
        </div>
      </div>
      <div className="type-cloud graph-legend">
        {entityTypeEntries.length ? (
          entityTypeEntries.map(([key, value]) => (
            <button
              className="legend-chip"
              key={key}
              type="button"
              onClick={() => onEntityTypeSelect(key)}
            >
              <span
                className="legend-dot"
                style={{ backgroundColor: resolveEntityTypeColor(key) }}
              />
              <span>{key}</span>
              <strong>{value}</strong>
            </button>
          ))
        ) : (
          <div className="empty-inline">点击节点或关系后，这里会展开详细信息。</div>
        )}
      </div>
    </div>
  );
}

function EntityDetails({
  payload,
  onClose,
  onEntitySelect,
  onRelationSelect,
  onFocusGraph,
}: {
  payload: GraphEntityDetail;
  onClose: () => void;
  onEntitySelect: GraphDetailsPanelProps["onEntitySelect"];
  onRelationSelect: GraphDetailsPanelProps["onRelationSelect"];
  onFocusGraph: GraphDetailsPanelProps["onFocusGraph"];
}) {
  const node = asRecord(payload.node);
  const title = entityTitle(node, payload.entity_id);
  const type = entityType(node);
  const description = getString(node, ["description"], "");

  return (
    <div className="stack detail-panel">
      <DetailHeader title={title} subtitle={`实体 · ${payload.workspace}`} onClose={onClose} />
      <div className="detail-summary-card">
        <TypeBadge type={type} />
        <div className="detail-metrics">
          <span>邻居 {payload.neighbors.length}</span>
          <span>关系 {payload.relations.length}</span>
        </div>
        {description ? <p>{description}</p> : null}
      </div>
      <div className="drawer-actions">
        <button className="toolbar-button" type="button" onClick={() => onFocusGraph(title)}>
          定位到图中
        </button>
      </div>
      <KeyValueList title="实体属性" record={node} />
      {payload.neighbors.length ? (
        <div className="detail-block">
          <h4>相邻实体</h4>
          <div className="entity-link-list">
            {payload.neighbors.slice(0, 24).map((neighbor) => {
              const neighborNode = asRecord(neighbor.node);
              const neighborTitle = entityTitle(neighborNode, neighbor.entity_id);
              return (
                <button
                  className="entity-link-row"
                  key={neighbor.entity_id}
                  type="button"
                  onClick={() => {
                    onFocusGraph(neighborTitle);
                    onEntitySelect(payload.workspace, neighbor.entity_id);
                  }}
                >
                  <span>{neighborTitle}</span>
                  <TypeBadge type={entityType(neighborNode)} />
                </button>
              );
            })}
          </div>
        </div>
      ) : null}
      {payload.relations.length ? (
        <div className="detail-block">
          <h4>直接关系</h4>
          <div className="relation-list">
            {payload.relations.slice(0, 18).map((relation) => {
              const edge = asRecord(relation.edge);
              const peer =
                relation.source === payload.entity_id ? relation.target : relation.source;
              return (
                <button
                  className="relation-row"
                  key={`${relation.source}-${relation.target}-${relationTitle(edge)}`}
                  type="button"
                  onClick={() =>
                    onRelationSelect(payload.workspace, relation.source, relation.target)
                  }
                >
                  <strong>{peer}</strong>
                  <span>{relationTitle(edge)}</span>
                </button>
              );
            })}
          </div>
        </div>
      ) : null}
    </div>
  );
}

function RelationDetails({
  payload,
  pathLoading,
  onClose,
  onEntitySelect,
  onPathExplain,
  onFocusGraph,
}: {
  payload: GraphRelationDetail;
  pathLoading?: boolean;
  onClose: () => void;
  onEntitySelect: GraphDetailsPanelProps["onEntitySelect"];
  onPathExplain: GraphDetailsPanelProps["onPathExplain"];
  onFocusGraph: GraphDetailsPanelProps["onFocusGraph"];
}) {
  const edge = asRecord(payload.edge);
  const sourceNode = asRecord(payload.source_node);
  const targetNode = asRecord(payload.target_node);
  const sourceTitle = entityTitle(sourceNode, payload.source);
  const targetTitle = entityTitle(targetNode, payload.target);
  const description = getString(edge, ["description"], "");

  return (
    <div className="stack detail-panel">
      <DetailHeader
        title={`${sourceTitle} → ${targetTitle}`}
        subtitle={`关系 · ${payload.workspace}`}
        onClose={onClose}
      />
      <div className="detail-summary-card">
        <span className="tag">{relationTitle(edge)}</span>
        {description ? <p>{description}</p> : null}
      </div>
      <div className="drawer-actions">
        <button
          className="primary-button"
          type="button"
          disabled={pathLoading}
          onClick={() =>
            onPathExplain({
              workspace: payload.workspace,
              source: payload.source,
              target: payload.target,
            })
          }
        >
          {pathLoading ? "解释中..." : "路径解释"}
        </button>
        <button className="toolbar-button" type="button" onClick={() => onFocusGraph(sourceTitle)}>
          定位来源
        </button>
        <button className="toolbar-button" type="button" onClick={() => onFocusGraph(targetTitle)}>
          定位目标
        </button>
      </div>
      <div className="endpoint-grid">
        <button
          className="endpoint-card"
          type="button"
          onClick={() => onEntitySelect(payload.workspace, payload.source)}
        >
          <span>来源实体</span>
          <strong>{sourceTitle}</strong>
          <TypeBadge type={entityType(sourceNode)} />
        </button>
        <button
          className="endpoint-card"
          type="button"
          onClick={() => onEntitySelect(payload.workspace, payload.target)}
        >
          <span>目标实体</span>
          <strong>{targetTitle}</strong>
          <TypeBadge type={entityType(targetNode)} />
        </button>
      </div>
      <KeyValueList title="关系属性" record={edge} />
    </div>
  );
}

function PathDetails({
  payload,
  onClose,
  onFocusGraph,
}: {
  payload: GraphPathResponse;
  onClose: () => void;
  onFocusGraph: GraphDetailsPanelProps["onFocusGraph"];
}) {
  const explanation = payload.path_explanation?.final_explanation;
  const uncertainty = payload.path_explanation?.uncertainty;
  return (
    <div className="stack detail-panel">
      <DetailHeader
        title={`${payload.source} ↔ ${payload.target}`}
        subtitle={`路径解释 · ${payload.workspace}`}
        onClose={onClose}
      />
      <div className="mini-card path-explanation">
        {explanation || "未找到可解释路径。"}
      </div>
      {uncertainty ? <div className="empty-inline">不确定性：{uncertainty}</div> : null}
      <div className="drawer-actions">
        <button className="toolbar-button" type="button" onClick={() => onFocusGraph(payload.source)}>
          定位起点
        </button>
        <button className="toolbar-button" type="button" onClick={() => onFocusGraph(payload.target)}>
          定位终点
        </button>
      </div>
      {payload.paths.length ? (
        <div className="detail-block">
          <h4>候选路径</h4>
          <div className="path-list">
            {payload.paths.map((path, index) => (
              <div className="path-row" key={`${path.path_text}-${index}`}>
                <span>路径 {index + 1}</span>
                <strong>{path.path_text}</strong>
                <small>
                  {path.nodes.length} 节点 · {path.edges.length} 边
                </small>
              </div>
            ))}
          </div>
        </div>
      ) : null}
    </div>
  );
}

export function GraphDetailsPanel({
  drawer,
  nodeCount,
  edgeCount,
  entityTypeEntries,
  pathLoading,
  onClose,
  onEntityTypeSelect,
  onEntitySelect,
  onRelationSelect,
  onPathExplain,
  onFocusGraph,
}: GraphDetailsPanelProps) {
  if (drawer.kind === "entity") {
    return (
      <EntityDetails
        payload={drawer.payload as GraphEntityDetail}
        onClose={onClose}
        onEntitySelect={onEntitySelect}
        onRelationSelect={onRelationSelect}
        onFocusGraph={onFocusGraph}
      />
    );
  }

  if (drawer.kind === "relation") {
    return (
      <RelationDetails
        payload={drawer.payload as GraphRelationDetail}
        pathLoading={pathLoading}
        onClose={onClose}
        onEntitySelect={onEntitySelect}
        onPathExplain={onPathExplain}
        onFocusGraph={onFocusGraph}
      />
    );
  }

  if (drawer.kind === "path") {
    return (
      <PathDetails
        payload={drawer.payload as GraphPathResponse}
        onClose={onClose}
        onFocusGraph={onFocusGraph}
      />
    );
  }

  if (drawer.kind === "error") {
    return (
      <div className="stack">
        <button className="ghost-button" type="button" onClick={onClose}>
          关闭
        </button>
        <div className="error-state">{String(drawer.payload ?? "加载失败")}</div>
      </div>
    );
  }

  return (
    <EmptyDetails
      nodeCount={nodeCount}
      edgeCount={edgeCount}
      entityTypeEntries={entityTypeEntries}
      onEntityTypeSelect={onEntityTypeSelect}
    />
  );
}
