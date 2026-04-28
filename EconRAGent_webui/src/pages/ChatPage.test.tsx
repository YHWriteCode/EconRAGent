import { fireEvent, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ChatPage } from "./ChatPage";
import { renderWithProviders } from "../test/utils";
import { useAppStore } from "../store/useAppStore";

const apiMocks = vi.hoisted(() => ({
  getSessionMessages: vi.fn(),
  listWorkspaces: vi.fn(),
  readSkillArtifactContent: vi.fn(),
  streamChat: vi.fn(),
  uploadFile: vi.fn(),
}));

vi.mock("../lib/api", () => ({
  getSessionMessages: apiMocks.getSessionMessages,
  listWorkspaces: apiMocks.listWorkspaces,
  readSkillArtifactContent: apiMocks.readSkillArtifactContent,
  streamChat: apiMocks.streamChat,
  uploadFile: apiMocks.uploadFile,
}));

describe("ChatPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useAppStore.setState({
      ...useAppStore.getState(),
      currentWorkspaceId: "",
      currentSessionId: "",
      localUserId: "webui-user-test",
      userAccountId: "",
      userDisplayName: "",
      memoryEnabled: true,
      queryMode: "hybrid",
      webSearchMode: "auto",
      pendingAttachments: [],
      messagesBySession: {},
    });
  });

  it("shows workspace display name instead of internal workspace id in the composer chip", async () => {
    apiMocks.getSessionMessages.mockResolvedValue({ session_id: "draft", messages: [] });
    apiMocks.listWorkspaces.mockResolvedValue({
      workspaces: [
        {
          workspace_id: "test__7f3b81",
          display_name: "test",
          description: null,
          created_at: "2026-04-27T10:00:00+08:00",
          updated_at: "2026-04-27T10:00:00+08:00",
          document_count: 3,
          node_count: 12,
          source_count: 1,
          last_updated_at: "2026-04-27T10:00:00+08:00",
          archived: false,
        },
      ],
    });

    useAppStore.setState({
      ...useAppStore.getState(),
      currentWorkspaceId: "test__7f3b81",
      currentSessionId: "draft",
      messagesBySession: { draft: [] },
    });

    renderWithProviders(<ChatPage />);

    await waitFor(() => {
      expect(screen.getByRole("button", { name: /test/ })).toBeInTheDocument();
    });
    expect(screen.queryByText("test__7f3b81")).not.toBeInTheDocument();
  });

  it("sends the all-workspaces sentinel when no single workspace is selected", async () => {
    apiMocks.getSessionMessages.mockResolvedValue({ session_id: "draft", messages: [] });
    apiMocks.listWorkspaces.mockResolvedValue({ workspaces: [] });
    apiMocks.streamChat.mockResolvedValue({
      answer: "答案",
      route: {},
      tool_calls: [],
      path_explanation: null,
      metadata: {},
      streaming_supported: true,
    });

    useAppStore.setState({
      ...useAppStore.getState(),
      currentWorkspaceId: "",
      currentSessionId: "draft",
      localUserId: "webui-user-test",
      userAccountId: "",
      memoryEnabled: true,
      messagesBySession: { draft: [] },
    });

    renderWithProviders(<ChatPage />);

    fireEvent.change(screen.getByPlaceholderText("有什么问题尽管问我..."), {
      target: { value: "规模经济是什么？" },
    });
    fireEvent.click(screen.getByRole("button", { name: "发送" }));

    await waitFor(() => {
      expect(apiMocks.streamChat).toHaveBeenCalledWith(
        expect.objectContaining({
          workspace: "all",
          user_id: "webui-user-test",
          use_memory: true,
        }),
        expect.any(Object),
      );
    });
  });

  it("lets the user disable memory for a chat request", async () => {
    apiMocks.getSessionMessages.mockResolvedValue({ session_id: "draft", messages: [] });
    apiMocks.listWorkspaces.mockResolvedValue({ workspaces: [] });
    apiMocks.streamChat.mockResolvedValue({
      answer: "答案",
      route: {},
      tool_calls: [],
      path_explanation: null,
      metadata: {},
      streaming_supported: true,
    });

    useAppStore.setState({
      ...useAppStore.getState(),
      currentWorkspaceId: "",
      currentSessionId: "draft",
      localUserId: "webui-user-test",
      userAccountId: "",
      memoryEnabled: true,
      messagesBySession: { draft: [] },
    });

    renderWithProviders(<ChatPage />);

    fireEvent.click(screen.getByRole("button", { name: "打开上传和联网搜索菜单" }));
    fireEvent.click(screen.getByRole("button", { name: "关闭记忆" }));
    fireEvent.change(screen.getByPlaceholderText("有什么问题尽管问我..."), {
      target: { value: "继续刚才的问题" },
    });
    fireEvent.click(screen.getByRole("button", { name: "发送" }));

    await waitFor(() => {
      expect(apiMocks.streamChat).toHaveBeenCalledWith(
        expect.objectContaining({
          user_id: undefined,
          use_memory: false,
        }),
        expect.any(Object),
      );
    });
  });

  it("uses the logged-in account id for memory-backed chat requests", async () => {
    apiMocks.getSessionMessages.mockResolvedValue({ session_id: "draft", messages: [] });
    apiMocks.listWorkspaces.mockResolvedValue({ workspaces: [] });
    apiMocks.streamChat.mockResolvedValue({
      answer: "答案",
      route: {},
      tool_calls: [],
      path_explanation: null,
      metadata: {},
      streaming_supported: true,
    });

    useAppStore.setState({
      ...useAppStore.getState(),
      currentWorkspaceId: "",
      currentSessionId: "draft",
      localUserId: "webui-user-test",
      userAccountId: "hang-yi",
      userDisplayName: "hang yi",
      memoryEnabled: true,
      messagesBySession: { draft: [] },
    });

    renderWithProviders(<ChatPage />);

    fireEvent.change(screen.getByPlaceholderText("有什么问题尽管问我..."), {
      target: { value: "继续刚才的问题" },
    });
    fireEvent.click(screen.getByRole("button", { name: "发送" }));

    await waitFor(() => {
      expect(apiMocks.streamChat).toHaveBeenCalledWith(
        expect.objectContaining({
          user_id: "hang-yi",
          use_memory: true,
        }),
        expect.any(Object),
      );
    });
  });

  it("renders streaming assistant output and metadata badges", async () => {
    apiMocks.getSessionMessages.mockResolvedValue({ session_id: "draft", messages: [] });
    apiMocks.listWorkspaces.mockResolvedValue({
      workspaces: [
        {
          workspace_id: "macro",
          display_name: "宏观研究",
          description: null,
          created_at: "2026-04-21T10:00:00+08:00",
          updated_at: "2026-04-21T10:00:00+08:00",
          document_count: 1,
          node_count: 1,
          source_count: 1,
          last_updated_at: "2026-04-21T10:00:00+08:00",
          archived: false,
        },
      ],
    });
    apiMocks.uploadFile.mockResolvedValue({
      upload_id: "upload-1",
      upload: {
        upload_id: "upload-1",
        filename: "memo.txt",
        stored_path: "/tmp/memo.txt",
        content_type: "text/plain",
        size_bytes: 4,
        created_at: "2026-04-21T10:00:00+08:00",
        kind: "file",
      },
    });
    apiMocks.streamChat.mockImplementation(async (_payload, handlers) => {
      handlers.onEvent?.({ type: "delta", content: "第一段" });
      handlers.onEvent?.({
        type: "meta",
        metadata: {
          effective_query_mode: "hybrid",
          web_search_forced: true,
        },
      });
      handlers.onEvent?.({
        type: "tool_result",
        tool_call: { name: "web_search" },
      });
      return {
        answer: "第一段\n最终答案",
        route: {},
        tool_calls: [{ name: "web_search" }],
        path_explanation: null,
        metadata: {
          effective_query_mode: "hybrid",
          web_search_forced: true,
        },
        streaming_supported: true,
      };
    });

    useAppStore.setState({
      ...useAppStore.getState(),
      currentWorkspaceId: "macro",
      currentSessionId: "draft",
      messagesBySession: { draft: [] },
    });

    renderWithProviders(<ChatPage />);

    fireEvent.click(screen.getByRole("button", { name: "打开上传和联网搜索菜单" }));
    fireEvent.click(screen.getByRole("button", { name: "开启" }));
    fireEvent.change(screen.getByPlaceholderText("有什么问题尽管问我..."), {
      target: { value: "请总结今天的宏观新闻" },
    });
    fireEvent.click(screen.getByRole("button", { name: "发送" }));

    await waitFor(() => {
      expect(apiMocks.streamChat).toHaveBeenCalledWith(
        expect.objectContaining({
          force_web_search: true,
          query_mode: "hybrid",
        }),
        expect.any(Object),
      );
    });

    await waitFor(() => {
      expect(
        screen.getByText((content) => /第一段\s*最终答案/.test(content)),
      ).toBeInTheDocument();
    });
    expect(screen.getByText("检索模式 hybrid")).toBeInTheDocument();
    expect(screen.getByText("联网强制开启")).toBeInTheDocument();
    expect(screen.getByText("工具调用 1 次")).toBeInTheDocument();
    expect(screen.getAllByText("联网搜索").length).toBeGreaterThan(0);
  });

  it("rejects unsupported .doc chat attachments and keeps the picker limited to supported formats", async () => {
    apiMocks.getSessionMessages.mockResolvedValue({ session_id: "draft", messages: [] });
    apiMocks.listWorkspaces.mockResolvedValue({ workspaces: [] });

    useAppStore.setState({
      ...useAppStore.getState(),
      currentWorkspaceId: "",
      currentSessionId: "draft",
      messagesBySession: { draft: [] },
    });

    const { container } = renderWithProviders(<ChatPage />);
    const input = container.querySelector('input[type="file"]') as HTMLInputElement | null;
    const acceptedTypes = (input?.getAttribute("accept") ?? "")
      .split(",")
      .map((value) => value.trim());

    expect(input).not.toBeNull();
    expect(acceptedTypes).toContain(".docx");
    expect(acceptedTypes).not.toContain(".doc");

    const legacyDoc = new File(["legacy"], "legacy.doc", {
      type: "application/msword",
    });
    fireEvent.change(input as HTMLInputElement, {
      target: { files: [legacyDoc] },
    });

    await waitFor(() => {
      expect(
        screen.getByText(
          "当前对话附件支持 PDF、DOCX、Markdown、EPUB 和常见文本文件；暂不支持 .doc。",
        ),
      ).toBeInTheDocument();
    });
    expect(apiMocks.uploadFile).not.toHaveBeenCalled();
  });
});
