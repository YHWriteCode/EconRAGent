import { fireEvent, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { GraphPage } from "./GraphPage";
import { renderWithProviders } from "../test/utils";
import { useAppStore } from "../store/useAppStore";

const apiMocks = vi.hoisted(() => ({
  explainGraphPath: vi.fn(),
  getGraphData: vi.fn(),
  getGraphEntityDetail: vi.fn(),
  getGraphRelationDetail: vi.fn(),
  getGraphSchema: vi.fn(),
  listWorkspaces: vi.fn(),
  searchGraphLabels: vi.fn(),
}));

vi.mock("../lib/api", () => ({
  explainGraphPath: apiMocks.explainGraphPath,
  getGraphData: apiMocks.getGraphData,
  getGraphEntityDetail: apiMocks.getGraphEntityDetail,
  getGraphRelationDetail: apiMocks.getGraphRelationDetail,
  getGraphSchema: apiMocks.getGraphSchema,
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
    apiMocks.getGraphSchema.mockResolvedValue({
      profile_name: "economy",
      domain_name: "economy",
      entity_types: [
        {
          name: "Company",
          display_name: "公司",
          description: "",
          aliases: [],
        },
        {
          name: "Event",
          display_name: "事件",
          description: "",
          aliases: [],
        },
      ],
      relation_types: [
        {
          name: "affects_metric",
          display_name: "影响指标",
          description: "",
          aliases: [],
          source_types: [],
          target_types: [],
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
        maxNodes: 400,
        entityType: "",
        relationType: "",
        timeFrom: "",
        timeTo: "",
      });
    });

    fireEvent.click(await screen.findByRole("button", { name: "宏观研究" }));
    fireEvent.click(screen.getByRole("button", { name: "应用筛选" }));

    await waitFor(() => {
      expect(useAppStore.getState().currentWorkspaceId).toBe("macro");
      expect(apiMocks.getGraphData).toHaveBeenLastCalledWith({
        workspace: "macro",
        label: "*",
        maxDepth: 2,
        maxNodes: 400,
        entityType: "",
        relationType: "",
        timeFrom: "",
        timeTo: "",
      });
    });

    fireEvent.click(await screen.findByRole("button", { name: "事件" }));
    fireEvent.click(screen.getByRole("button", { name: "应用筛选" }));

    await waitFor(() => {
      expect(apiMocks.getGraphData).toHaveBeenLastCalledWith({
        workspace: "macro",
        label: "*",
        maxDepth: 2,
        maxNodes: 400,
        entityType: "Event",
        relationType: "",
        timeFrom: "",
        timeTo: "",
      });
    });

    fireEvent.click(screen.getByRole("button", { name: "影响指标" }));
    fireEvent.click(screen.getByRole("button", { name: "应用筛选" }));

    await waitFor(() => {
      expect(apiMocks.getGraphData).toHaveBeenLastCalledWith({
        workspace: "macro",
        label: "*",
        maxDepth: 2,
        maxNodes: 400,
        entityType: "Event",
        relationType: "affects_metric",
        timeFrom: "",
        timeTo: "",
      });
    });
  });
});
