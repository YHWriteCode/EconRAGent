import { fireEvent, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { GraphPage } from "./GraphPage";
import { renderWithProviders } from "../test/utils";

const apiMocks = vi.hoisted(() => ({
  explainGraphPath: vi.fn(),
  getGraphData: vi.fn(),
  getGraphEntityDetail: vi.fn(),
  getGraphRelationDetail: vi.fn(),
  listWorkspaces: vi.fn(),
  searchGraphLabels: vi.fn(),
}));

vi.mock("../lib/api", () => ({
  explainGraphPath: apiMocks.explainGraphPath,
  getGraphData: apiMocks.getGraphData,
  getGraphEntityDetail: apiMocks.getGraphEntityDetail,
  getGraphRelationDetail: apiMocks.getGraphRelationDetail,
  listWorkspaces: apiMocks.listWorkspaces,
  searchGraphLabels: apiMocks.searchGraphLabels,
}));

describe("GraphPage", () => {
  it("reissues graph requests when filters change", async () => {
    apiMocks.listWorkspaces.mockResolvedValue({
      workspaces: [
        {
          workspace_id: "macro",
          display_name: "宏观研究",
        },
      ],
    });
    apiMocks.searchGraphLabels.mockResolvedValue({ items: [] });
    apiMocks.getGraphData.mockResolvedValue({
      workspace: "all",
      nodes: [],
      edges: [],
      summary: {
        node_count: 0,
        edge_count: 0,
        entity_type_counts: {},
        is_truncated: false,
      },
      is_truncated: false,
    });

    renderWithProviders(<GraphPage />, { route: "/graph" });

    await waitFor(() => {
      expect(apiMocks.getGraphData).toHaveBeenCalledWith({
        workspace: "all",
        label: "*",
        maxDepth: 2,
        maxNodes: 800,
        entityType: "",
        timeFrom: "",
        timeTo: "",
      });
    });

    fireEvent.click(screen.getByRole("button", { name: "事件" }));
    fireEvent.click(screen.getByRole("button", { name: "应用筛选" }));

    await waitFor(() => {
      expect(apiMocks.getGraphData).toHaveBeenLastCalledWith({
        workspace: "all",
        label: "*",
        maxDepth: 2,
        maxNodes: 800,
        entityType: "Event",
        timeFrom: "",
        timeTo: "",
      });
    });
  });
});
