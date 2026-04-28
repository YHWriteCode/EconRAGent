import { describe, expect, it } from "vitest";

import { useAppStore } from "./useAppStore";

describe("useAppStore", () => {
  it("persists the watchlist locally", () => {
    useAppStore.getState().addWatchlistTicker("amd");

    const raw = localStorage.getItem("econragent.webui.v1");
    expect(raw).toBeTruthy();
    expect(raw).toContain("AMD");
  });

  it("persists the local memory identity and toggle", () => {
    useAppStore.setState({
      ...useAppStore.getState(),
      localUserId: "webui-user-test",
    });
    useAppStore.getState().setUserAccount("hang-yi", "hang yi");
    useAppStore.getState().setMemoryEnabled(false);

    const raw = localStorage.getItem("econragent.webui.v1");
    expect(raw).toBeTruthy();
    expect(raw).toContain("webui-user-test");
    expect(raw).toContain("hang-yi");
    expect(raw).toContain("hang yi");
    expect(raw).toContain('"memoryEnabled":false');
  });
});
