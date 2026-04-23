import "@testing-library/jest-dom/vitest";
import { afterEach, beforeEach, vi } from "vitest";
import { cleanup } from "@testing-library/react";

import { useAppStore } from "../store/useAppStore";

const initialState = useAppStore.getState();

class MockIntersectionObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}

class MockResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}

beforeEach(() => {
  cleanup();
  localStorage.clear();
  useAppStore.setState({
    ...initialState,
    currentWorkspaceId: "",
    currentSessionId: "",
    queryMode: "hybrid",
    webSearchMode: "auto",
    watchlist: ["NVDA", "AAPL", "MSFT", "TSLA", "BTC"],
    pendingAttachments: [],
    messagesBySession: {},
  });
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    value: vi.fn().mockImplementation((query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })),
  });
  vi.stubGlobal("IntersectionObserver", MockIntersectionObserver);
  vi.stubGlobal("ResizeObserver", MockResizeObserver);
  vi.stubGlobal("confirm", vi.fn(() => true));
});

afterEach(() => {
  vi.restoreAllMocks();
});
