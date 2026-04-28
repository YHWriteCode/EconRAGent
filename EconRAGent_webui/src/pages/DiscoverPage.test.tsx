import { fireEvent, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { DiscoverPage } from "./DiscoverPage";
import { renderWithProviders } from "../test/utils";
import { useAppStore } from "../store/useAppStore";

const apiMocks = vi.hoisted(() => ({
  listDiscoverEvents: vi.fn(),
}));

vi.mock("../lib/api", () => ({
  listDiscoverEvents: apiMocks.listDiscoverEvents,
}));

describe("DiscoverPage", () => {
  it("appends the next page when load more is clicked", async () => {
    apiMocks.listDiscoverEvents
      .mockResolvedValueOnce({
        items: [
          {
            event_id: "source-1:cluster-1",
            workspace: "macro",
            source_id: "source-1",
            cluster_id: "cluster-1",
            category: "macro",
            headline: "美联储释放新信号",
            summary: "第一条摘要",
            published_at: "2026-04-21T09:00:00+08:00",
            updated_at: "2026-04-21T09:00:00+08:00",
            sort_time: "2026-04-21T09:00:00+08:00",
            source_count: 1,
            sources: [
              {
                url: "https://example.com/1",
                domain: "example.com",
                label: "E",
                favicon_url: null,
              },
            ],
          },
        ],
        next_cursor: "2026-04-21T09:00:00+08:00|source-1:cluster-1",
      })
      .mockResolvedValueOnce({
        items: [
          {
            event_id: "source-2:cluster-2",
            workspace: "macro",
            source_id: "source-2",
            cluster_id: "cluster-2",
            category: "macro",
            headline: "二级市场出现异动",
            summary: "第二条摘要",
            published_at: "2026-04-21T08:00:00+08:00",
            updated_at: "2026-04-21T08:00:00+08:00",
            sort_time: "2026-04-21T08:00:00+08:00",
            source_count: 1,
            sources: [
              {
                url: "https://example.com/2",
                domain: "example.com",
                label: "E",
                favicon_url: null,
              },
            ],
          },
        ],
        next_cursor: null,
      });
    renderWithProviders(<DiscoverPage />);

    await screen.findByText("美联储释放新信号");
    expect(screen.getByText("市场行情")).toBeInTheDocument();
    expect(screen.getAllByText("1 个来源").length).toBeGreaterThan(0);

    fireEvent.click(screen.getByRole("button", { name: "加载更多" }));

    await waitFor(() => {
      expect(screen.getByText("二级市场出现异动")).toBeInTheDocument();
    });
    expect(screen.getByText("查看全部市场")).toBeInTheDocument();
  });

  it("uses the summary as a card title when the backend headline is empty", async () => {
    apiMocks.listDiscoverEvents.mockResolvedValueOnce({
      items: [
        {
          event_id: "source-1:cluster-empty-title",
          workspace: "macro",
          source_id: "source-1",
          cluster_id: "cluster-empty-title",
          category: "macro",
          headline: "",
          summary: "摘要首句可作为标题。后续内容留在摘要区域。",
          published_at: "2026-04-21T09:00:00+08:00",
          updated_at: "2026-04-21T09:00:00+08:00",
          sort_time: "2026-04-21T09:00:00+08:00",
          source_count: 1,
          sources: [
            {
              url: "https://example.com/1",
              domain: "example.com",
              label: "E",
              favicon_url: null,
            },
          ],
        },
      ],
      next_cursor: null,
    });

    renderWithProviders(<DiscoverPage />);

    await screen.findByText("摘要首句可作为标题。");
    expect(screen.queryByText("未命名事件")).not.toBeInTheDocument();
  });
});
