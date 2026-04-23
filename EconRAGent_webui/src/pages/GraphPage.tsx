import { Suspense, lazy, useDeferredValue, useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import type { Core } from "cytoscape";
import { useSearchParams } from "react-router-dom";

import { getGraphData, listWorkspaces, searchGraphLabels } from "../lib/api";
import { toIsoDateTime } from "../lib/format";
import {
  resolveGraphNodeLabel,
  resolveGraphNodeSearchText,
} from "../lib/graphVisual";
import { queryKeys } from "../lib/queryKeys";
import type { GraphFilters } from "../types";
import type { GraphLayoutName } from "../components/CytoscapeGraph";

const CytoscapeGraph = lazy(async () => {
  const module = await import("../components/CytoscapeGraph");
  return { default: module.CytoscapeGraph };
});

const ENTITY_FILTERS = [
  { label: "公司", value: "Company", icon: "▦" },
  { label: "行业", value: "Industry", icon: "⌘" },
  { label: "指标", value: "Metric", icon: "▥" },
  { label: "政策", value: "Policy", icon: "▣" },
  { label: "事件", value: "Event", icon: "▤" },
  { label: "国家", value: "Country", icon: "◎" },
] as const;

const RELATION_FILTERS = [
  { label: "影响", value: "impact", icon: "→" },
  { label: "属于", value: "belongs_to", icon: "⊂" },
  { label: "关联", value: "related", icon: "↔" },
  { label: "导致", value: "causes", icon: "↪" },
  { label: "竞争", value: "competes", icon: "⚔" },
] as const;

function createInitialFilters(workspaceFromUrl?: string | null): GraphFilters {
  return {
    workspace: workspaceFromUrl || "all",
    label: "*",
    maxDepth: 2,
    maxNodes: 120,
    entityType: "",
    timeFrom: "",
    timeTo: "",
  };
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
  const [relationFilter, setRelationFilter] = useState("");
  const [filterCollapsed, setFilterCollapsed] = useState(false);
  const [focusRequest, setFocusRequest] = useState({ query: "", nonce: 0 });
  const cyRef = useRef<Core | null>(null);
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
    setRelationFilter("");
    setGraphSearchText("");
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
                showEdgeLabels
                onEdgeSelect={() => undefined}
                onNodeSelect={() => undefined}
                onReady={(instance) => {
                  cyRef.current = instance;
                }}
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

          <div className="graph-line-legend" aria-label="关系图例">
            <span><i className="line-solid" />影响</span>
            <span><i className="line-muted" />属于</span>
            <span><i className="line-dotted" />关联</span>
            <span><i className="line-dashed" />导致</span>
          </div>
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
                  <span className="filter-check" aria-hidden="true" />
                  {workspace.label}
                </button>
              ))}
            </div>
          </section>

          <section className="filter-panel">
            <h3>实体类型</h3>
            <div className="filter-chip-grid">
              {ENTITY_FILTERS.map((entity) => (
                <button
                  className={`filter-chip ${
                    draftFilters.entityType === entity.value ? "active" : ""
                  }`}
                  key={entity.value}
                  type="button"
                  onClick={() =>
                    setDraftFilters((current) => ({
                      ...current,
                      entityType: current.entityType === entity.value ? "" : entity.value,
                    }))
                  }
                >
                  <span className="filter-icon" aria-hidden="true">{entity.icon}</span>
                  {entity.label}
                </button>
              ))}
            </div>
          </section>

          <section className="filter-panel">
            <h3>关系类型</h3>
            <div className="filter-chip-grid relation-chip-grid">
              {RELATION_FILTERS.map((relation) => (
                <button
                  className={`filter-chip ${relationFilter === relation.value ? "active" : ""}`}
                  key={relation.value}
                  type="button"
                  onClick={() =>
                    setRelationFilter((current) =>
                      current === relation.value ? "" : relation.value,
                    )
                  }
                >
                  <span className="filter-icon" aria-hidden="true">{relation.icon}</span>
                  {relation.label}
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
