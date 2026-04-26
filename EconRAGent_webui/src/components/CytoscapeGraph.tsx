import { useEffect, useRef } from "react";
import cytoscape, {
  type Core,
  type ElementDefinition,
  type EventObject,
  type StylesheetJson,
} from "cytoscape";

import type { GraphPayload } from "../types";
import {
  resolveEntityTypeColor,
  resolveGraphEdgeLabel,
  resolveGraphEdgeWeight,
  resolveGraphNodeKey,
  resolveGraphNodeLabel,
  resolveGraphNodeSearchText,
  resolveGraphNodeType,
} from "../lib/graphVisual";

export type GraphLayoutName = "cose" | "circle" | "grid" | "concentric" | "breadthfirst";

interface CytoscapeGraphProps {
  graph: GraphPayload | null;
  layoutName: GraphLayoutName;
  focusQuery?: string;
  focusNonce?: number;
  showEdgeLabels?: boolean;
  onNodeSelect: (workspaceId: string, entityId: string) => void;
  onEdgeSelect: (workspaceId: string, source: string, target: string) => void;
  onReady: (cy: Core | null) => void;
}

const MIN_NODE_SIZE = 34;
const MAX_NODE_SIZE = 94;
const MIN_EDGE_SIZE = 1.1;
const MAX_EDGE_SIZE = 5.4;

function createGraphStyle(showEdgeLabels: boolean): StylesheetJson {
  return [
  {
    selector: "node",
    style: {
      label: "data(label)",
        "background-color": "data(color)",
        "background-opacity": 0.94,
        color: "#2a2117",
      "font-size": "12px",
      "font-weight": 600,
      "text-wrap": "wrap",
        "text-max-width": "76px",
      "text-valign": "center",
      "text-halign": "center",
      "text-margin-y": "0px",
        width: "data(size)",
        height: "data(size)",
      "border-width": "3px",
      "border-color": "#fffaf5",
        "shadow-blur": 18,
        "shadow-color": "rgba(63, 51, 40, 0.16)",
        "shadow-opacity": 0.7,
      "overlay-opacity": 0,
    },
  },
  {
    selector: "edge",
    style: {
        width: "data(width)",
        "line-color": "rgba(77, 74, 68, 0.38)",
        "target-arrow-color": "rgba(77, 74, 68, 0.44)",
      "target-arrow-shape": "triangle",
      "curve-style": "bezier",
        label: showEdgeLabels ? "data(label)" : "",
      "font-size": "9px",
      color: "#6b5a46",
      "text-background-color": "rgba(255, 250, 242, 0.92)",
      "text-background-opacity": 1,
      "text-background-padding": "2px",
      "text-rotation": "autorotate",
      "overlay-opacity": 0,
    },
  },
    {
      selector: ".dimmed",
      style: {
        opacity: 0.16,
        "text-opacity": 0.12,
      },
    },
    {
      selector: "node.neighborhood",
      style: {
        opacity: 1,
        "text-opacity": 1,
        "border-width": "3px",
        "border-color": "#f6d365",
      },
    },
    {
      selector: "node.selected",
      style: {
        "border-width": "4px",
        "border-color": "#c05621",
        "shadow-blur": 24,
        "shadow-color": "rgba(192, 86, 33, 0.34)",
      },
    },
    {
      selector: "edge.neighborhood",
      style: {
        opacity: 0.95,
        "line-color": "#c05621",
        "target-arrow-color": "#c05621",
      },
    },
    {
      selector: "edge.selected",
      style: {
        width: 5,
        "line-color": "#9a3412",
        "target-arrow-color": "#9a3412",
      },
    },
  ] as unknown as StylesheetJson;
}

function resolveLayout(name: GraphLayoutName) {
  if (name === "circle") {
    return { name: "circle", fit: true, padding: 36, animate: false };
  }
  if (name === "grid") {
    return { name: "grid", fit: true, padding: 36, animate: false };
  }
  if (name === "concentric") {
    return { name: "concentric", fit: true, padding: 42, animate: false, minNodeSpacing: 26 };
  }
  if (name === "breadthfirst") {
    return { name: "breadthfirst", fit: true, padding: 42, animate: false, directed: true };
  }
  return {
    name: "cose",
    fit: true,
    padding: 44,
    animate: false,
    nodeRepulsion: 9000,
    idealEdgeLength: 118,
    gravity: 0.18,
  };
}

function scaleValue(value: number, min: number, max: number, outMin: number, outMax: number) {
  if (max <= min) {
    return Math.round((outMin + outMax) / 2);
  }
  return outMin + (outMax - outMin) * Math.sqrt((value - min) / (max - min));
}

function buildElements(graph: GraphPayload | null): ElementDefinition[] {
  const nodes = graph?.nodes ?? [];
  const edges = graph?.edges ?? [];
  const nodeDegree = new Map<string, number>();
  const edgeWeights = edges.map(resolveGraphEdgeWeight);
  const minWeight = Math.min(...edgeWeights, 1);
  const maxWeight = Math.max(...edgeWeights, 1);

  for (const node of nodes) {
    nodeDegree.set(resolveGraphNodeKey(node), 0);
  }

  for (const edge of edges) {
    const sourceKey = `${edge.workspace_id}::${edge.source}`;
    const targetKey = `${edge.workspace_id}::${edge.target}`;
    if (!nodeDegree.has(sourceKey) || !nodeDegree.has(targetKey)) {
      continue;
    }
    nodeDegree.set(sourceKey, (nodeDegree.get(sourceKey) ?? 0) + 1);
    nodeDegree.set(targetKey, (nodeDegree.get(targetKey) ?? 0) + 1);
  }

  const degrees = Array.from(nodeDegree.values());
  const minDegree = Math.min(...degrees, 0);
  const maxDegree = Math.max(...degrees, 0);
  const nodeEntityIdMap = new Map<string, string>();
  for (const node of nodes) {
    nodeEntityIdMap.set(String(node.id), resolveGraphNodeLabel(node));
  }

  return [
    ...nodes.map((node) => {
      const key = resolveGraphNodeKey(node);
      const entityType = resolveGraphNodeType(node);
      const degree = nodeDegree.get(key) ?? 0;
      return {
        data: {
          id: key,
          label: resolveGraphNodeLabel(node),
          workspaceId: node.workspace_id,
          entityId: nodeEntityIdMap.get(String(node.id)) ?? String(node.id),
          entityType,
          color: resolveEntityTypeColor(entityType),
          size: scaleValue(degree, minDegree, maxDegree, MIN_NODE_SIZE, MAX_NODE_SIZE),
          degree,
          searchText: resolveGraphNodeSearchText(node),
        },
      };
    }),
    ...edges.map((edge, index) => {
      const weight = resolveGraphEdgeWeight(edge);
      return {
        data: {
          id: `${edge.workspace_id}::${edge.id || `${edge.source}::${edge.target}`}::${index}`,
          source: `${edge.workspace_id}::${edge.source}`,
          target: `${edge.workspace_id}::${edge.target}`,
          label: resolveGraphEdgeLabel(edge),
          workspaceId: edge.workspace_id,
          sourceEntityId:
            nodeEntityIdMap.get(String(edge.source)) ?? String(edge.source),
          targetEntityId:
            nodeEntityIdMap.get(String(edge.target)) ?? String(edge.target),
          weight,
          width: scaleValue(weight, minWeight, maxWeight, MIN_EDGE_SIZE, MAX_EDGE_SIZE),
        },
      };
    }),
  ];
}

function clearFocus(cy: Core) {
  cy.elements().removeClass("dimmed neighborhood selected");
}

function focusNode(cy: Core, nodeId: string, shouldFit = true) {
  const node = cy.getElementById(nodeId);
  if (node.empty()) {
    return;
  }
  const neighborhood = node.closedNeighborhood();
  clearFocus(cy);
  cy.elements().not(neighborhood).addClass("dimmed");
  neighborhood.addClass("neighborhood");
  node.addClass("selected");
  if (shouldFit) {
    cy.animate({ fit: { eles: neighborhood, padding: 86 }, duration: 260 });
  }
}

function focusEdge(cy: Core, edgeId: string) {
  const edge = cy.getElementById(edgeId);
  if (edge.empty()) {
    return;
  }
  const connected = edge.connectedNodes().union(edge);
  clearFocus(cy);
  cy.elements().not(connected).addClass("dimmed");
  connected.addClass("neighborhood");
  edge.addClass("selected");
  cy.animate({ fit: { eles: connected, padding: 96 }, duration: 260 });
}

export function CytoscapeGraph({
  graph,
  layoutName,
  focusQuery,
  focusNonce,
  showEdgeLabels = false,
  onNodeSelect,
  onEdgeSelect,
  onReady,
}: CytoscapeGraphProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const cyRef = useRef<Core | null>(null);
  const selectedRef = useRef<{ kind: "node" | "edge"; id: string } | null>(null);

  useEffect(() => {
    if (!containerRef.current) {
      return;
    }

    const cy = cytoscape({
      container: containerRef.current,
      elements: buildElements(graph),
      style: createGraphStyle(showEdgeLabels),
      minZoom: 0.3,
      maxZoom: 2.6,
      wheelSensitivity: 0.2,
    });

    cy.on("mouseover", "node", (event: EventObject) => {
      if (!selectedRef.current) {
        focusNode(cy, event.target.id(), false);
      }
    });

    cy.on("mouseout", "node", () => {
      if (!selectedRef.current) {
        clearFocus(cy);
      }
    });

    cy.on("tap", "node", (event: EventObject) => {
      const node = event.target.data();
      selectedRef.current = { kind: "node", id: event.target.id() };
      focusNode(cy, event.target.id());
      onNodeSelect(String(node.workspaceId), String(node.entityId));
    });

    cy.on("tap", "edge", (event: EventObject) => {
      const edge = event.target.data();
      selectedRef.current = { kind: "edge", id: event.target.id() };
      focusEdge(cy, event.target.id());
      onEdgeSelect(
        String(edge.workspaceId),
        String(edge.sourceEntityId),
        String(edge.targetEntityId),
      );
    });

    cy.on("tap", (event: EventObject) => {
      if (event.target === cy) {
        selectedRef.current = null;
        clearFocus(cy);
      }
    });

    cy.layout(resolveLayout(layoutName)).run();
    cyRef.current = cy;
    onReady(cy);

    return () => {
      onReady(null);
      cyRef.current = null;
      selectedRef.current = null;
      cy.destroy();
    };
  }, [graph, layoutName, onEdgeSelect, onNodeSelect, onReady, showEdgeLabels]);

  useEffect(() => {
    const cy = cyRef.current;
    const query = focusQuery?.trim().toLowerCase();
    if (!cy || !query) {
      return;
    }

    const matches = cy.nodes().filter((element) =>
      String(element.data("searchText") || "").includes(query),
    );
    const match = matches[0];
    if (!match) {
      return;
    }

    selectedRef.current = { kind: "node", id: match.id() };
    focusNode(cy, match.id());
  }, [focusQuery, focusNonce]);

  return <div className="cytoscape-canvas" ref={containerRef} />;
}
