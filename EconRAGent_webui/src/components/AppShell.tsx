import { useEffect, useMemo } from "react";
import { NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useShallow } from "zustand/react/shallow";

import { deleteSession, listSessions, listWorkspaces } from "../lib/api";
import { truncate } from "../lib/format";
import { queryKeys } from "../lib/queryKeys";
import { useAppStore } from "../store/useAppStore";
import type { SessionSummary } from "../types";

function buildDraftSession(
  currentSessionId: string,
  messages: ReturnType<typeof useAppStore.getState>["messagesBySession"],
): SessionSummary | null {
  if (!currentSessionId) {
    return null;
  }
  const currentMessages = messages[currentSessionId] ?? [];
  const firstUser = currentMessages.find((message) => message.role === "user");
  const lastMessage = currentMessages[currentMessages.length - 1];
  return {
    session_id: currentSessionId,
    workspace:
      typeof firstUser?.metadata.workspace === "string"
        ? firstUser.metadata.workspace
        : null,
    title: firstUser?.content ? truncate(firstUser.content, 32) : "新建聊天",
    created_at: firstUser?.timestamp ?? new Date().toISOString(),
    last_message_at: lastMessage?.timestamp ?? new Date().toISOString(),
    message_count: currentMessages.length,
    last_message_preview: lastMessage?.content
      ? truncate(lastMessage.content, 54)
      : "等待首条消息",
  };
}

export function AppShell() {
  const navigate = useNavigate();
  const location = useLocation();
  const queryClient = useQueryClient();
  const {
    currentWorkspaceId,
    currentSessionId,
    messagesBySession,
    setCurrentWorkspaceId,
    setCurrentSessionId,
    createDraftSession,
    removeSession,
  } = useAppStore(
    useShallow((state) => ({
      currentWorkspaceId: state.currentWorkspaceId,
      currentSessionId: state.currentSessionId,
      messagesBySession: state.messagesBySession,
      setCurrentWorkspaceId: state.setCurrentWorkspaceId,
      setCurrentSessionId: state.setCurrentSessionId,
      createDraftSession: state.createDraftSession,
      removeSession: state.removeSession,
    })),
  );

  const workspacesQuery = useQuery({
    queryKey: queryKeys.workspaces,
    queryFn: listWorkspaces,
  });
  const sessionsQuery = useQuery({
    queryKey: queryKeys.sessions(currentWorkspaceId),
    queryFn: () => listSessions(currentWorkspaceId || undefined),
  });

  const deleteSessionMutation = useMutation({
    mutationFn: deleteSession,
    onSuccess: (_, sessionId) => {
      removeSession(sessionId);
      queryClient.invalidateQueries({
        queryKey: queryKeys.sessions(currentWorkspaceId),
      });
      if (currentSessionId === sessionId) {
        const nextSessionId = createDraftSession();
        setCurrentSessionId(nextSessionId);
        navigate("/chat");
      }
    },
  });

  useEffect(() => {
    const items = workspacesQuery.data?.workspaces ?? [];
    if (!items.length) {
      return;
    }
    if (
      currentWorkspaceId &&
      !items.some((item) => item.workspace_id === currentWorkspaceId)
    ) {
      setCurrentWorkspaceId("");
    }
  }, [currentWorkspaceId, setCurrentWorkspaceId, workspacesQuery.data?.workspaces]);

  useEffect(() => {
    if (!currentSessionId) {
      createDraftSession();
    }
  }, [createDraftSession, currentSessionId]);

  const currentWorkspace = useMemo(() => {
    const items = workspacesQuery.data?.workspaces ?? [];
    return items.find((item) => item.workspace_id === currentWorkspaceId) ?? null;
  }, [currentWorkspaceId, workspacesQuery.data?.workspaces]);

  const sidebarSessions = useMemo(() => {
    const remoteSessions = sessionsQuery.data?.sessions ?? [];
    const draftSession = buildDraftSession(currentSessionId, messagesBySession);
    if (!draftSession) {
      return remoteSessions;
    }
    if (remoteSessions.some((item) => item.session_id === draftSession.session_id)) {
      return remoteSessions;
    }
    return [draftSession, ...remoteSessions];
  }, [currentSessionId, messagesBySession, sessionsQuery.data?.sessions]);
  const isSpacesRoute = location.pathname.startsWith("/spaces");

  return (
    <div className="shell">
      <aside className="sidebar">
        <header className="sidebar-header">
          <button
            className="brand-mark"
            type="button"
            aria-label="返回对话首页"
            onClick={() => navigate("/chat")}
          >
            <span className="brand-symbol">ER</span>
            <span className="brand-name">EconRAGent</span>
          </button>
        </header>

        <button
          className="sidebar-create-button"
          type="button"
          aria-label="新建聊天"
          onClick={() => {
            const sessionId = createDraftSession();
            setCurrentSessionId(sessionId);
            navigate("/chat");
          }}
        >
          <span>+</span>
          <span>新建聊天</span>
        </button>

        <section className="sidebar-group">
          <button
            className={`sidebar-section-link sidebar-space-button ${
              isSpacesRoute ? "active" : ""
            }`}
            type="button"
            onClick={() => navigate("/spaces")}
          >
            <span className="sidebar-space-icon" aria-hidden="true">
              ◇
            </span>
            <span>空间</span>
          </button>
          <div className="sidebar-subtitle">
            {currentWorkspace
              ? `${currentWorkspace.display_name} · ${currentWorkspace.document_count} 文档`
              : "管理知识库空间"}
          </div>
        </section>

        <section className="sidebar-group sidebar-history">
          <div className="sidebar-label">历史会话</div>
          <div className="session-list compact-session-list">
            {sidebarSessions.length ? (
              sidebarSessions.map((session) => (
                <div
                  className={`session-item ${
                    currentSessionId === session.session_id ? "active" : ""
                  }`}
                  key={session.session_id}
                >
                  <button
                    className="session-item-main"
                    type="button"
                    aria-label={`打开会话 ${session.title}`}
                    onClick={() => {
                      setCurrentSessionId(session.session_id);
                      navigate("/chat");
                    }}
                  >
                    <span className="session-chat-icon" aria-hidden="true" />
                    <span className="session-item-title">{session.title}</span>
                  </button>
                  <button
                    className="ghost-button icon-button session-delete"
                    type="button"
                    aria-label={`删除 ${session.title}`}
                    onClick={() => deleteSessionMutation.mutate(session.session_id)}
                  >
                    ···
                  </button>
                </div>
              ))
            ) : (
              <div className="empty-inline">还没有历史会话。</div>
            )}
          </div>
        </section>
      </aside>

      <div className="app-main">
        {!isSpacesRoute ? (
          <header className="topbar">
            <nav className="topnav" aria-label="Primary">
              <NavLink
                className={({ isActive }) =>
                  `nav-button ${isActive ? "active" : ""}`.trim()
                }
                to="/graph"
              >
                <span className="nav-icon" aria-hidden="true">⌘</span>
                知识图谱
              </NavLink>
              <NavLink
                className={({ isActive }) =>
                  `nav-button ${isActive ? "active" : ""}`.trim()
                }
                to="/chat"
              >
                <span className="nav-icon" aria-hidden="true">○</span>
                对话
              </NavLink>
              <NavLink
                className={({ isActive }) =>
                  `nav-button ${isActive ? "active" : ""}`.trim()
                }
                to="/discover"
              >
                <span className="nav-icon" aria-hidden="true">◎</span>
                发现
              </NavLink>
            </nav>
            <button className="notification-button" type="button" aria-label="通知">
              ♢
            </button>
          </header>
        ) : null}

        <main className={`content-shell route-${location.pathname.split("/").pop()}`}>
          <Outlet />
        </main>
      </div>
    </div>
  );
}
