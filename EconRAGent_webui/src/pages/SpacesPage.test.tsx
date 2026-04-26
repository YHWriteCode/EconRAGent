import { fireEvent, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

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
  beforeEach(() => {
    vi.clearAllMocks();
  });

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

  it("limits workspace file imports to supported document formats", async () => {
    apiMocks.listWorkspaces.mockResolvedValue({
      workspaces: [
        {
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
        },
      ],
    });
    apiMocks.uploadFile.mockResolvedValue({
      upload_id: "upload-1",
      upload: {
        upload_id: "upload-1",
        filename: "report.epub",
      },
    });
    apiMocks.createWorkspaceImport.mockResolvedValue({
      status: "accepted",
      workspace_id: "macro",
      track_id: "track-1",
    });
    apiMocks.getImportStatus.mockResolvedValue({
      track_id: "track-1",
      workspace_id: "macro",
      document_count: 1,
      status_counts: { processed: 1 },
      documents: [],
    });

    const { container } = renderWithProviders(<SpacesPage />, { route: "/spaces" });

    fireEvent.click(await screen.findByRole("button", { name: "宏观研究 操作菜单" }));
    fireEvent.click(screen.getByRole("button", { name: /导入文件/ }));

    expect(screen.getByText("支持 Word（.docx）、PDF、Markdown（.md）和 EPUB")).toBeInTheDocument();

    const fileInput = container.querySelector('input[type="file"]') as HTMLInputElement;
    expect(fileInput.accept).toContain(".epub");
    expect(fileInput.accept).toContain(".docx");

    fireEvent.change(fileInput, {
      target: {
        files: [
          new File(["epub"], "report.epub", {
            type: "application/epub+zip",
          }),
        ],
      },
    });
    fireEvent.click(screen.getByRole("button", { name: "提交" }));

    expect(screen.getByRole("button", { name: "提交中..." })).toBeDisabled();
    expect(screen.getByRole("status")).toHaveTextContent("正在上传文件");

    await waitFor(() => {
      expect(apiMocks.uploadFile).toHaveBeenCalled();
      expect(apiMocks.createWorkspaceImport).toHaveBeenCalledWith("macro", {
        kind: "upload",
        upload_id: "upload-1",
      });
    });
    expect(await screen.findByText("导入提交成功")).toBeInTheDocument();
  });
});
