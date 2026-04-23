import { describe, expect, it } from "vitest";

import { useAppStore } from "./useAppStore";

describe("useAppStore", () => {
  it("persists the watchlist locally", () => {
    useAppStore.getState().addWatchlistTicker("amd");

    const raw = localStorage.getItem("econragent.webui.v1");
    expect(raw).toBeTruthy();
    expect(raw).toContain("AMD");
  });
});
