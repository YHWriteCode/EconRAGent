import { fireEvent, screen, waitFor } from "@testing-library/react";
import { Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { AppShell } from "./components/AppShell";
import { renderWithProviders } from "./test/utils";
import { useAppStore } from "./store/useAppStore";

const apiMocks = vi.hoisted(() => ({
  listWorkspaces: vi.fn(),
  listSessions: vi.fn(),
  deleteSession: vi.fn(),
}));

vi.mock("./lib/api", () => ({
  listWorkspaces: apiMocks.listWorkspaces,
  listSessions: apiMocks.listSessions,
  deleteSession: apiMocks.deleteSession,
}));

describe("AppShell", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useAppStore.setState({
      ...useAppStore.getState(),
      currentWorkspaceId: "",
      currentSessionId: "",
      localUserId: "webui-user-local",
      userAccountId: "",
      userDisplayName: "",
      memoryEnabled: true,
      pendingAttachments: [],
      messagesBySession: {},
    });
  });

  it("switches routes and highlights the active tab", async () => {
    apiMocks.listWorkspaces.mockResolvedValue({
      workspaces: [
        {
          workspace_id: "macro",
          display_name: "宏观研究",
          description: null,
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
    apiMocks.listSessions.mockResolvedValue({ sessions: [] });
    apiMocks.deleteSession.mockResolvedValue({ status: "deleted", session_id: "old" });

    renderWithProviders(
      <Routes>
        <Route path="/" element={<AppShell />}>
          <Route path="chat" element={<div>chat page</div>} />
          <Route path="graph" element={<div>graph page</div>} />
          <Route path="discover" element={<div>discover page</div>} />
          <Route path="spaces" element={<div>spaces page</div>} />
        </Route>
      </Routes>,
      { route: "/chat" },
    );

    await screen.findByText("chat page");
    expect(screen.getByRole("link", { name: "对话" })).toHaveAttribute(
      "aria-current",
      "page",
    );

    fireEvent.click(screen.getByRole("link", { name: "发现" }));
    await screen.findByText("discover page");
    expect(screen.getByRole("link", { name: "发现" })).toHaveAttribute(
      "aria-current",
      "page",
    );
  });

  it("creates a new draft session from the sidebar button", async () => {
    apiMocks.listWorkspaces.mockResolvedValue({
      workspaces: [
        {
          workspace_id: "macro",
          display_name: "宏观研究",
          description: null,
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
    apiMocks.listSessions.mockResolvedValue({ sessions: [] });

    renderWithProviders(
      <Routes>
        <Route path="/" element={<AppShell />}>
          <Route path="chat" element={<div>chat page</div>} />
        </Route>
      </Routes>,
      { route: "/chat" },
    );

    await screen.findByText("chat page");
    const before = useAppStore.getState().currentSessionId;

    fireEvent.click(screen.getByRole("button", { name: "新建聊天" }));

    await waitFor(() => {
      expect(useAppStore.getState().currentSessionId).not.toBe(before);
    });
  });

  it("saves a sidebar account id for memory-scoped sessions", async () => {
    apiMocks.listWorkspaces.mockResolvedValue({ workspaces: [] });
    apiMocks.listSessions.mockResolvedValue({ sessions: [] });

    renderWithProviders(
      <Routes>
        <Route path="/" element={<AppShell />}>
          <Route path="chat" element={<div>chat page</div>} />
        </Route>
      </Routes>,
      { route: "/chat" },
    );

    await screen.findByText("chat page");
    fireEvent.click(screen.getByRole("button", { name: "用户登录与记忆设置" }));
    fireEvent.change(screen.getByPlaceholderText("例如 hang-yi"), {
      target: { value: "hang-yi" },
    });
    fireEvent.change(screen.getByPlaceholderText("例如 hang yi"), {
      target: { value: "hang yi" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));

    await waitFor(() => {
      expect(useAppStore.getState().userAccountId).toBe("hang-yi");
    });
    expect(screen.getByRole("button", { name: "用户登录与记忆设置" })).toHaveTextContent(
      "hang yi",
    );
    await waitFor(() => {
      expect(apiMocks.listSessions).toHaveBeenLastCalledWith(undefined, "hang-yi");
    });
  });
});
