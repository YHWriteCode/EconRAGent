import { fireEvent, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { SpacesPage } from "./SpacesPage";
import { renderWithProviders } from "../test/utils";

const apiMocks = vi.hoisted(() => ({
  createWorkspace: vi.fn(),
  createWorkspaceImport: vi.fn(),
  deleteWorkspace: vi.fn(),
  getImportStatus: vi.fn(),
  listWorkspaces: vi.fn(),
  updateWorkspace: vi.fn(),
  uploadFile: vi.fn(),
}));

vi.mock("../lib/api", () => ({
  createWorkspace: apiMocks.createWorkspace,
  createWorkspaceImport: apiMocks.createWorkspaceImport,
  deleteWorkspace: apiMocks.deleteWorkspace,
  getImportStatus: apiMocks.getImportStatus,
  listWorkspaces: apiMocks.listWorkspaces,
  updateWorkspace: apiMocks.updateWorkspace,
  uploadFile: apiMocks.uploadFile,
}));

describe("SpacesPage", () => {
  it("creates a workspace from the create modal", async () => {
    apiMocks.listWorkspaces.mockResolvedValue({ workspaces: [] });
    apiMocks.getImportStatus.mockResolvedValue({
      track_id: "track-1",
      workspace_id: "macro",
      document_count: 1,
      status_counts: { done: 1 },
      documents: [],
    });
    apiMocks.createWorkspace.mockResolvedValue({
      workspace_id: "macro",
      display_name: "宏观研究",
      description: "观察全球宏观主题",
      created_at: "2026-04-21T10:00:00+08:00",
      updated_at: "2026-04-21T10:00:00+08:00",
      document_count: 0,
      node_count: 0,
      source_count: 0,
      last_updated_at: "2026-04-21T10:00:00+08:00",
      archived: false,
    });

    renderWithProviders(<SpacesPage />, { route: "/spaces" });

    fireEvent.click(screen.getAllByRole("button", { name: "新建空间" })[0]!);

    fireEvent.change(screen.getByPlaceholderText("显示名称"), {
      target: { value: "宏观研究" },
    });
    fireEvent.change(screen.getByPlaceholderText("描述（可选）"), {
      target: { value: "观察全球宏观主题" },
    });
    fireEvent.click(screen.getByRole("button", { name: "提交" }));

    await waitFor(() => {
      expect(apiMocks.createWorkspace).toHaveBeenCalled();
      expect(apiMocks.createWorkspace.mock.calls[0]?.[0]).toEqual({
        display_name: "宏观研究",
        description: "观察全球宏观主题",
      });
    });
  });
});
