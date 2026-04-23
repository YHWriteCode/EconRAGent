import { create } from "zustand";
import { createJSONStorage, persist } from "zustand/middleware";

import type { ChatMessage, QueryMode, UploadRecord, WebSearchMode } from "../types";

const DEFAULT_WATCHLIST = ["NVDA", "AAPL", "MSFT", "TSLA", "BTC"];

export function createSessionId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `session-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

type MessageUpdater = (message: ChatMessage) => ChatMessage;

interface AppState {
  currentWorkspaceId: string;
  currentSessionId: string;
  queryMode: QueryMode;
  webSearchMode: WebSearchMode;
  watchlist: string[];
  pendingAttachments: UploadRecord[];
  messagesBySession: Record<string, ChatMessage[]>;
  setCurrentWorkspaceId: (workspaceId: string) => void;
  setCurrentSessionId: (sessionId: string) => void;
  createDraftSession: () => string;
  setQueryMode: (mode: QueryMode) => void;
  setWebSearchMode: (value: WebSearchMode) => void;
  addWatchlistTicker: (ticker: string) => void;
  removeWatchlistTicker: (ticker: string) => void;
  setSessionMessages: (sessionId: string, messages: ChatMessage[]) => void;
  appendMessages: (sessionId: string, messages: ChatMessage[]) => void;
  updateMessage: (
    sessionId: string,
    clientId: string,
    updater: MessageUpdater,
  ) => void;
  removeSession: (sessionId: string) => void;
  addPendingAttachment: (upload: UploadRecord) => void;
  removePendingAttachment: (uploadId: string) => void;
  clearPendingAttachments: () => void;
}

function normalizeWatchlist(values: string[]): string[] {
  const next: string[] = [];
  const seen = new Set<string>();
  for (const value of values) {
    const normalized = value.trim().toUpperCase();
    if (!normalized || seen.has(normalized)) {
      continue;
    }
    next.push(normalized);
    seen.add(normalized);
  }
  return next;
}

function ensureSessionBucket(
  messagesBySession: Record<string, ChatMessage[]>,
  sessionId: string,
) {
  return messagesBySession[sessionId] ? { ...messagesBySession } : {
    ...messagesBySession,
    [sessionId]: [],
  };
}

export const useAppStore = create<AppState>()(
  persist(
    (set, get) => ({
      currentWorkspaceId: "",
      currentSessionId: "",
      queryMode: "hybrid",
      webSearchMode: "auto",
      watchlist: DEFAULT_WATCHLIST,
      pendingAttachments: [],
      messagesBySession: {},
      setCurrentWorkspaceId: (workspaceId) => {
        const normalized = workspaceId.trim();
        set((state) => {
          if (state.currentWorkspaceId === normalized) {
            return state;
          }
          return {
            currentWorkspaceId: normalized,
            currentSessionId: "",
            pendingAttachments: [],
          };
        });
      },
      setCurrentSessionId: (sessionId) => {
        const normalized = sessionId.trim();
        set((state) => ({
          currentSessionId: normalized,
          messagesBySession: ensureSessionBucket(state.messagesBySession, normalized),
        }));
      },
      createDraftSession: () => {
        const nextSessionId = createSessionId();
        set((state) => ({
          currentSessionId: nextSessionId,
          pendingAttachments: [],
          messagesBySession: {
            ...state.messagesBySession,
            [nextSessionId]: [],
          },
        }));
        return nextSessionId;
      },
      setQueryMode: (mode) => set({ queryMode: mode }),
      setWebSearchMode: (value) => set({ webSearchMode: value }),
      addWatchlistTicker: (ticker) => {
        set((state) => ({
          watchlist: normalizeWatchlist([...state.watchlist, ticker]),
        }));
      },
      removeWatchlistTicker: (ticker) => {
        const normalized = ticker.trim().toUpperCase();
        set((state) => ({
          watchlist: state.watchlist.filter((item) => item !== normalized),
        }));
      },
      setSessionMessages: (sessionId, messages) => {
        set((state) => ({
          messagesBySession: {
            ...state.messagesBySession,
            [sessionId]: [...messages],
          },
        }));
      },
      appendMessages: (sessionId, messages) => {
        set((state) => ({
          messagesBySession: {
            ...state.messagesBySession,
            [sessionId]: [...(state.messagesBySession[sessionId] ?? []), ...messages],
          },
        }));
      },
      updateMessage: (sessionId, clientId, updater) => {
        set((state) => ({
          messagesBySession: {
            ...state.messagesBySession,
            [sessionId]: (state.messagesBySession[sessionId] ?? []).map((message) =>
              message.clientId === clientId ? updater(message) : message,
            ),
          },
        }));
      },
      removeSession: (sessionId) => {
        set((state) => {
          const nextMessages = { ...state.messagesBySession };
          delete nextMessages[sessionId];
          const nextSessionId =
            state.currentSessionId === sessionId ? "" : state.currentSessionId;
          return {
            currentSessionId: nextSessionId,
            messagesBySession: nextMessages,
          };
        });
      },
      addPendingAttachment: (upload) => {
        set((state) => ({
          pendingAttachments: [
            ...state.pendingAttachments,
            upload,
          ].filter(
            (item, index, array) =>
              array.findIndex((candidate) => candidate.upload_id === item.upload_id) ===
              index,
          ),
        }));
      },
      removePendingAttachment: (uploadId) => {
        set((state) => ({
          pendingAttachments: state.pendingAttachments.filter(
            (item) => item.upload_id !== uploadId,
          ),
        }));
      },
      clearPendingAttachments: () => set({ pendingAttachments: [] }),
    }),
    {
      name: "econragent.webui.v1",
      storage: createJSONStorage(() => localStorage),
      partialize: (state) => ({
        currentWorkspaceId: state.currentWorkspaceId,
        currentSessionId: state.currentSessionId,
        queryMode: state.queryMode,
        webSearchMode: state.webSearchMode,
        watchlist: state.watchlist,
      }),
      merge: (persistedState, currentState) => ({
        ...currentState,
        ...(persistedState as Partial<AppState>),
      }),
    },
  ),
);

export function currentSessionMessages(sessionId: string): ChatMessage[] {
  return useAppStore.getState().messagesBySession[sessionId] ?? [];
}

export function ensureDraftSession(): string {
  const store = useAppStore.getState();
  if (store.currentSessionId) {
    store.setCurrentSessionId(store.currentSessionId);
    return store.currentSessionId;
  }
  return store.createDraftSession();
}
