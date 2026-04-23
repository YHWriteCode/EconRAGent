import { render } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { CytoscapeGraph } from "./CytoscapeGraph";

const cytoscapeState = vi.hoisted(() => ({
  elements: [] as Array<{ data: Record<string, unknown> }>,
  handlers: new Map<string, (event: { target: { data: () => Record<string, unknown> } }) => void>(),
}));

function createCollection() {
  const collection = {
    empty: vi.fn(() => false),
    closedNeighborhood: vi.fn(() => collection),
    connectedNodes: vi.fn(() => collection),
    union: vi.fn(() => collection),
    addClass: vi.fn(() => collection),
    removeClass: vi.fn(() => collection),
    not: vi.fn(() => collection),
  };
  return collection;
}

vi.mock("cytoscape", () => ({
  default: vi.fn((config: { elements: Array<{ data: Record<string, unknown> }> }) => {
    cytoscapeState.elements = config.elements;
    cytoscapeState.handlers = new Map();
    const collection = createCollection();
    return {
      on: vi.fn((eventName: string, selector: string | ((event: unknown) => void), handler?: (event: { target: { data: () => Record<string, unknown> } }) => void) => {
        if (typeof selector === "string" && handler) {
          cytoscapeState.handlers.set(`${eventName}:${selector}`, handler);
        }
      }),
      elements: vi.fn(() => collection),
      getElementById: vi.fn(() => collection),
      layout: vi.fn(() => ({
        run: vi.fn(),
      })),
      animate: vi.fn(),
      destroy: vi.fn(),
    };
  }),
}));

describe("CytoscapeGraph", () => {
  it("uses semantic entity ids for node and edge callbacks", () => {
    const onNodeSelect = vi.fn();
    const onEdgeSelect = vi.fn();

    render(
      <CytoscapeGraph
        graph={{
          workspace: "macro",
          nodes: [
            {
              id: "50",
              labels: ["美联储"],
              properties: { entity_id: "美联储", entity_type: "organization" },
              workspace_id: "macro",
            },
            {
              id: "48",
              labels: ["美元流动性"],
              properties: { entity_id: "美元流动性", entity_type: "concept" },
              workspace_id: "macro",
            },
          ],
          edges: [
            {
              id: "29",
              type: "DIRECTED",
              source: "48",
              target: "50",
              properties: {},
              workspace_id: "macro",
            },
          ],
          summary: {
            node_count: 2,
            edge_count: 1,
            entity_type_counts: {},
            is_truncated: false,
          },
          is_truncated: false,
        }}
        layoutName="cose"
        onEdgeSelect={onEdgeSelect}
        onNodeSelect={onNodeSelect}
        onReady={() => {}}
      />,
    );

    const nodeHandler = cytoscapeState.handlers.get("tap:node");
    const edgeHandler = cytoscapeState.handlers.get("tap:edge");
    const nodeElement = cytoscapeState.elements.find(
      (item) => item.data.entityId === "美联储",
    );
    const edgeElement = cytoscapeState.elements.find(
      (item) =>
        item.data.sourceEntityId === "美元流动性" &&
        item.data.targetEntityId === "美联储",
    );

    expect(nodeHandler).toBeTypeOf("function");
    expect(edgeHandler).toBeTypeOf("function");
    expect(nodeElement).toBeTruthy();
    expect(edgeElement).toBeTruthy();
    expect(nodeElement!.data.color).toBeTruthy();
    expect(Number(nodeElement!.data.size)).toBeGreaterThan(0);

    nodeHandler!({
      target: {
        id: () => String(nodeElement!.data.id),
        data: () => nodeElement!.data,
      },
    } as any);
    edgeHandler!({
      target: {
        id: () => String(edgeElement!.data.id),
        data: () => edgeElement!.data,
      },
    } as any);

    expect(onNodeSelect).toHaveBeenCalledWith("macro", "美联储");
    expect(onEdgeSelect).toHaveBeenCalledWith("macro", "美元流动性", "美联储");
  });
});
