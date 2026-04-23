import { fireEvent, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ChatPage } from "./ChatPage";
import { renderWithProviders } from "../test/utils";
import { useAppStore } from "../store/useAppStore";

const apiMocks = vi.hoisted(() => ({
  getSessionMessages: vi.fn(),
  streamChat: vi.fn(),
  uploadFile: vi.fn(),
}));

vi.mock("../lib/api", () => ({
  getSessionMessages: apiMocks.getSessionMessages,
  streamChat: apiMocks.streamChat,
  uploadFile: apiMocks.uploadFile,
}));

describe("ChatPage", () => {
  it("renders streaming assistant output and metadata badges", async () => {
    apiMocks.getSessionMessages.mockResolvedValue({ session_id: "draft", messages: [] });
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
});
