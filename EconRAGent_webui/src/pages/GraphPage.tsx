import {
  Suspense,
  lazy,
  useCallback,
  useDeferredValue,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { useQuery } from "@tanstack/react-query";
import type { Core } from "cytoscape";
import { useSearchParams } from "react-router-dom";

import {
  getGraphData,
  getGraphEntityDetail,
  getGraphRelationDetail,
  getGraphSchema,
  listWorkspaces,
  searchGraphLabels,
} from "../lib/api";
import { formatTime, toIsoDateTime } from "../lib/format";
import {
  resolveEntityTypeColor,
  resolveGraphNodeLabel,
  resolveGraphNodeSearchText,
} from "../lib/graphVisual";
import { queryKeys } from "../lib/queryKeys";
import type {
  GraphEntityDetail,
  GraphFilters,
  GraphRelationDetail,
  GraphSchemaOption,
} from "../types";
import type { GraphLayoutName } from "../components/CytoscapeGraph";

const CytoscapeGraph = lazy(async () => {
  const module = await import("../components/CytoscapeGraph");
  return { default: module.CytoscapeGraph };
});

type GraphInspectState =
  | { kind: "empty" }
  | { kind: "loading"; title: string }
  | { kind: "entity"; payload: GraphEntityDetail }
  | { kind: "relation"; payload: GraphRelationDetail }
  | { kind: "error"; message: string };

function createInitialFilters(workspaceFromUrl?: string | null): GraphFilters {
  return {
    workspace: workspaceFromUrl || "all",
    label: "*",
    maxDepth: 2,
    maxNodes: 800,
    entityType: "",
    relationType: "",
    timeFrom: "",
    timeTo: "",
  };
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function readText(record: Record<string, unknown>, keys: string[], fallback = "") {
  for (const key of keys) {
    const value = record[key];
    if (typeof value === "string" && value.trim()) {
      return value.trim().replace(/<SEP>/g, " / ");
    }
    if (typeof value === "number") {
      return String(value);
    }
  }
  return fallback;
}

function optionLabel(option: GraphSchemaOption): string {
  return option.display_name || option.name;
}

function chipLabelStyle(label: string) {
  const length = Array.from(label).length;
  if (length <= 4) {
    return undefined;
  }
  return {
    fontSize: length <= 6 ? "12px" : length <= 8 ? "11px" : "10px",
  };
}

function entityTitle(node: Record<string, unknown>, fallback: string) {
  return readText(node, ["entity_id", "entity_name", "name", "label", "id"], fallback);
}

function entityType(node: Record<string, unknown>) {
  return readText(node, ["entity_type", "type"], "unknown");
}

function relationTitle(edge: Record<string, unknown>) {
  return readText(edge, ["relation_type", "keywords", "relation", "type"], "关系");
}

function updatedAt(record: Record<string, unknown>) {
  return readText(record, ["last_confirmed_at", "updated_at", "created_at"]);
}

function GraphInspectPanel({
  detail,
  onClose,
}: {
  detail: GraphInspectState;
  onClose: () => void;
}) {
  if (detail.kind === "empty") {
    return null;
  }

  if (detail.kind === "loading") {
    return (
      <aside className="graph-inspect-panel">
        <div className="detail-header">
          <div>
            <span className="detail-kicker">详情</span>
            <h3>{detail.title}</h3>
          </div>
          <button className="ghost-button" type="button" onClick={onClose}>
            关闭
          </button>
        </div>
        <div className="empty-inline">正在加载...</div>
      </aside>
    );
  }

  if (detail.kind === "error") {
    return (
      <aside className="graph-inspect-panel">
        <div className="detail-header">
          <div>
            <span className="detail-kicker">详情</span>
            <h3>加载失败</h3>
          </div>
          <button className="ghost-button" type="button" onClick={onClose}>
            关闭
          </button>
        </div>
        <div className="error-state">{detail.message}</div>
      </aside>
    );
  }

  if (detail.kind === "entity") {
    const node = asRecord(detail.payload.node);
    const type = entityType(node);
    const source = readText(node, ["source_id", "source", "file_path", "doc_id"], "无");
    const description = readText(node, ["description"], "暂无描述");
    return (
      <aside className="graph-inspect-panel">
        <div className="detail-header">
          <div>
            <span className="detail-kicker">实体 · {detail.payload.workspace}</span>
            <h3>{entityTitle(node, detail.payload.entity_id)}</h3>
          </div>
          <button className="ghost-button" type="button" onClick={onClose}>
            关闭
          </button>
        </div>
        <div className="graph-inspect-summary">
          <span className="entity-type-badge">
            <span
              className="legend-dot"
              style={{ backgroundColor: resolveEntityTypeColor(type) }}
            />
            {type}
          </span>
          <p>{description}</p>
        </div>
        <dl className="graph-inspect-kv">
          <div>
            <dt>节点来源</dt>
            <dd>{source}</dd>
          </div>
          <div>
            <dt>节点更新时间</dt>
            <dd>{formatTime(updatedAt(node))}</dd>
          </div>
        </dl>
      </aside>
    );
  }

  const edge = asRecord(detail.payload.edge);
  const description = readText(edge, ["description"], "暂无描述");
  return (
    <aside className="graph-inspect-panel">
      <div className="detail-header">
        <div>
          <span className="detail-kicker">关系 · {detail.payload.workspace}</span>
          <h3>{detail.payload.source} → {detail.payload.target}</h3>
        </div>
        <button className="ghost-button" type="button" onClick={onClose}>
          关闭
        </button>
      </div>
      <dl className="graph-inspect-kv">
        <div>
          <dt>关系名</dt>
          <dd>{relationTitle(edge)}</dd>
        </div>
        <div>
          <dt>关系描述</dt>
          <dd>{description}</dd>
        </div>
      </dl>
    </aside>
  );
}

export function GraphPage() {
  const [searchParams] = useSearchParams();
  const initialWorkspace = searchParams.get("workspace");
  const [draftFilters, setDraftFilters] = useState<GraphFilters>(() =>
    createInitialFilters(initialWorkspace),
  );
  const [appliedFilters, setAppliedFilters] = useState<GraphFilters>(() =>
    createInitialFilters(initialWorkspace),
  );
  const [layoutName] = useState<GraphLayoutName>("cose");
  const [graphSearchText, setGraphSearchText] = useState("");
  const [filterCollapsed, setFilterCollapsed] = useState(false);
  const [focusRequest, setFocusRequest] = useState({ query: "", nonce: 0 });
  const [inspectDetail, setInspectDetail] = useState<GraphInspectState>({ kind: "empty" });
  const cyRef = useRef<Core | null>(null);
  const detailRequestRef = useRef(0);
  const deferredLabel = useDeferredValue(draftFilters.label);

  useEffect(() => {
    if (!initialWorkspace) {
      return;
    }
    setDraftFilters((current) => ({ ...current, workspace: initialWorkspace }));
    setAppliedFilters((current) => ({ ...current, workspace: initialWorkspace }));
  }, [initialWorkspace]);

  const workspacesQuery = useQuery({
    queryKey: queryKeys.workspaces,
    queryFn: listWorkspaces,
  });

  const graphSchemaQuery = useQuery({
    queryKey: queryKeys.graphSchema(draftFilters.workspace || "all"),
    queryFn: () => getGraphSchema(draftFilters.workspace || "all"),
  });

  const graphScope = JSON.stringify(appliedFilters);
  const graphQuery = useQuery({
    queryKey: queryKeys.graph(graphScope),
    queryFn: () =>
      getGraphData({
        ...appliedFilters,
        timeFrom: toIsoDateTime(appliedFilters.timeFrom) ?? "",
        timeTo: toIsoDateTime(appliedFilters.timeTo) ?? "",
      }),
  });

  const labelSuggestionsQuery = useQuery({
    queryKey: queryKeys.graphLabels(draftFilters.workspace, deferredLabel),
    queryFn: () =>
      searchGraphLabels(draftFilters.workspace || "all", deferredLabel.trim()),
    enabled: Boolean(deferredLabel.trim() && deferredLabel.trim() !== "*"),
  });

  const graphNodeOptions = useMemo(
    () =>
      (graphQuery.data?.nodes ?? [])
        .map((node) => ({
          label: resolveGraphNodeLabel(node),
          searchText: resolveGraphNodeSearchText(node),
        }))
        .sort((a, b) => a.label.localeCompare(b.label, "zh-CN"))
        .slice(0, 120),
    [graphQuery.data?.nodes],
  );

  const workspaceOptions = useMemo(
    () => [
      { id: "all", label: "全部" },
      ...(workspacesQuery.data?.workspaces ?? []).map((workspace) => ({
        id: workspace.workspace_id,
        label: workspace.display_name || workspace.workspace_id,
      })),
    ],
    [workspacesQuery.data?.workspaces],
  );

  const nodeSearchValue = draftFilters.label === "*" ? "" : draftFilters.label;

  const entityFilters = graphSchemaQuery.data?.entity_types ?? [];
  const relationFilters = graphSchemaQuery.data?.relation_types ?? [];

  const closeInspectDetail = useCallback(() => {
    detailRequestRef.current += 1;
    setInspectDetail({ kind: "empty" });
  }, []);

  const handleNodeSelect = useCallback(async (workspaceId: string, entityId: string) => {
    const requestId = detailRequestRef.current + 1;
    detailRequestRef.current = requestId;
    setInspectDetail({ kind: "loading", title: entityId });
    try {
      const payload = await getGraphEntityDetail(workspaceId, entityId);
      if (detailRequestRef.current === requestId) {
        setInspectDetail({ kind: "entity", payload });
      }
    } catch (error) {
      if (detailRequestRef.current === requestId) {
        setInspectDetail({
          kind: "error",
          message: error instanceof Error ? error.message : "节点详情加载失败",
        });
      }
    }
  }, []);

  const handleEdgeSelect = useCallback(
    async (workspaceId: string, source: string, target: string) => {
      const requestId = detailRequestRef.current + 1;
      detailRequestRef.current = requestId;
      setInspectDetail({ kind: "loading", title: `${source} → ${target}` });
      try {
        const payload = await getGraphRelationDetail(workspaceId, source, target);
        if (detailRequestRef.current === requestId) {
          setInspectDetail({ kind: "relation", payload });
        }
      } catch (error) {
        if (detailRequestRef.current === requestId) {
          setInspectDetail({
            kind: "error",
            message: error instanceof Error ? error.message : "关系详情加载失败",
          });
        }
      }
    },
    [],
  );

  const handleGraphReady = useCallback((instance: Core | null) => {
    cyRef.current = instance;
  }, []);

  const requestGraphFocus = () => {
    const query = graphSearchText.trim();
    if (!query) {
      return;
    }
    setFocusRequest((current) => ({ query, nonce: current.nonce + 1 }));
  };

  const applyFilters = () => {
    setAppliedFilters({
      ...draftFilters,
      label: draftFilters.label.trim() || "*",
    });
  };

  const resetFilters = () => {
    const next = createInitialFilters(initialWorkspace);
    setDraftFilters(next);
    setAppliedFilters(next);
    setGraphSearchText("");
    closeInspectDetail();
  };

  return (
    <div
      className={`graph-layout graph-explorer-layout ${
        filterCollapsed ? "graph-filter-collapsed" : ""
      }`}
    >
      <section className="graph-board graph-visual-board">
        <header className="graph-board-header">
          <div className="page-title">
            <h1>知识图谱视图</h1>
            <p>探索实体、关系与路径</p>
          </div>
          <div className="graph-board-search">
            <input
              className="input graph-top-search"
              list="graph-node-suggestions"
              placeholder="搜索节点、关系或路径"
              value={graphSearchText}
              onChange={(event) => setGraphSearchText(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") {
                  event.preventDefault();
                  requestGraphFocus();
                }
              }}
            />
            <datalist id="graph-node-suggestions">
              {graphNodeOptions.map((item) => (
                <option key={`${item.label}-${item.searchText}`} value={item.label} />
              ))}
            </datalist>
            <button
              className="graph-icon-button"
              type="button"
              aria-label="定位节点"
              onClick={requestGraphFocus}
            >
              ⌘
            </button>
            {filterCollapsed ? (
              <button
                className="graph-filter-toggle"
                type="button"
                onClick={() => setFilterCollapsed(false)}
              >
                展开筛选
              </button>
            ) : null}
          </div>
        </header>

        <div className="graph-stage graph-canvas-stage">
          {graphQuery.isLoading ? (
            <div className="empty-state">正在加载图谱...</div>
          ) : graphQuery.error ? (
            <div className="error-state">
              {graphQuery.error instanceof Error
                ? graphQuery.error.message
                : "图谱加载失败"}
            </div>
          ) : graphQuery.data?.nodes.length ? (
            <Suspense fallback={<div className="empty-state">正在加载图谱渲染器...</div>}>
              <CytoscapeGraph
                graph={graphQuery.data}
                layoutName={layoutName}
                focusQuery={focusRequest.query}
                focusNonce={focusRequest.nonce}
                onEdgeSelect={handleEdgeSelect}
                onNodeSelect={handleNodeSelect}
                onReady={handleGraphReady}
              />
            </Suspense>
          ) : (
            <div className="empty-state">当前筛选条件下没有可展示的节点。</div>
          )}

          <div className="graph-zoom-controls" aria-label="图谱缩放控制">
            <button type="button" onClick={() => cyRef.current?.zoom(cyRef.current.zoom() + 0.15)}>
              +
            </button>
            <button type="button" onClick={() => cyRef.current?.zoom(cyRef.current.zoom() - 0.15)}>
              −
            </button>
            <button type="button" onClick={() => cyRef.current?.fit()} aria-label="适配视图">
              ⛶
            </button>
          </div>

          <div className="graph-canvas-meta" aria-label="图谱统计">
            <span>节点 {graphQuery.data?.summary.node_count ?? 0}</span>
            <span>边 {graphQuery.data?.summary.edge_count ?? 0}</span>
          </div>
          <GraphInspectPanel detail={inspectDetail} onClose={closeInspectDetail} />
        </div>
      </section>

      {!filterCollapsed ? (
      <aside className="graph-filter-sidebar">
        <div className="graph-filter-sidebar-header">
          <h2>筛选数据库</h2>
          <button
            className="graph-filter-collapse-button"
            type="button"
            aria-label="收起筛选数据库"
            onClick={() => setFilterCollapsed(true)}
          >
            ‹
          </button>
        </div>
        <form
          className="graph-filter-form"
          onSubmit={(event) => {
            event.preventDefault();
            applyFilters();
          }}
        >
          <section className="filter-panel">
            <h3>数据库</h3>
            <div className="filter-chip-grid database-chip-grid">
              {workspaceOptions.map((workspace) => (
                <button
                  className={`filter-chip ${
                    draftFilters.workspace === workspace.id ? "active" : ""
                  }`}
                  key={workspace.id}
                  type="button"
                  onClick={() =>
                    setDraftFilters((current) => ({
                      ...current,
                      workspace: workspace.id,
                    }))
                  }
                >
                  <span
                    className="filter-chip-label"
                    style={chipLabelStyle(workspace.label)}
                  >
                    {workspace.label}
                  </span>
                </button>
              ))}
            </div>
          </section>

          <section className="filter-panel">
            <h3>实体类型</h3>
            <div className="filter-chip-grid">
              {entityFilters.map((entity) => (
                <button
                  className={`filter-chip ${
                    draftFilters.entityType === entity.name ? "active" : ""
                  }`}
                  key={entity.name}
                  type="button"
                  onClick={() =>
                    setDraftFilters((current) => ({
                      ...current,
                      entityType: current.entityType === entity.name ? "" : entity.name,
                    }))
                  }
                >
                  <span
                    className="filter-chip-label"
                    style={chipLabelStyle(optionLabel(entity))}
                  >
                    {optionLabel(entity)}
                  </span>
                </button>
              ))}
            </div>
          </section>

          <section className="filter-panel">
            <h3>关系类型</h3>
            <div className="filter-chip-grid relation-chip-grid">
              {relationFilters.map((relation) => (
                <button
                  className={`filter-chip ${
                    draftFilters.relationType === relation.name ? "active" : ""
                  }`}
                  key={relation.name}
                  type="button"
                  onClick={() =>
                    setDraftFilters((current) => ({
                      ...current,
                      relationType:
                        current.relationType === relation.name ? "" : relation.name,
                    }))
                  }
                >
                  <span
                    className="filter-chip-label"
                    style={chipLabelStyle(optionLabel(relation))}
                  >
                    {optionLabel(relation)}
                  </span>
                </button>
              ))}
            </div>
          </section>

          <section className="filter-panel filter-search-panel">
            <h3>搜索节点</h3>
            <label className="filter-search-input">
              <input
                list="graph-label-suggestions"
                placeholder="输入节点名称进行搜索"
                value={nodeSearchValue}
                onChange={(event) =>
                  setDraftFilters((current) => ({
                    ...current,
                    label: event.target.value || "*",
                  }))
                }
              />
              <button type="submit" aria-label="搜索节点">
                ⌕
              </button>
            </label>
            <datalist id="graph-label-suggestions">
              {(labelSuggestionsQuery.data?.items ?? []).map((item) => (
                <option key={`${item.workspace_id}-${item.label}`} value={item.label} />
              ))}
            </datalist>
          </section>

          <div className="graph-filter-actions">
            <button className="primary-button" type="submit">
              应用筛选
            </button>
            <button className="ghost-button" type="button" onClick={resetFilters}>
              重置
            </button>
          </div>
        </form>
      </aside>
      ) : null}
    </div>
  );
}
