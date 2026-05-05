import { useEffect, useMemo, useState } from "react";
import { NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useShallow } from "zustand/react/shallow";

import { Icon } from "./Icons";
import { Modal } from "./Modal";
import { deleteSession, listSessions, listWorkspaces } from "../lib/api";
import { truncate } from "../lib/format";
import { queryKeys } from "../lib/queryKeys";
import { useAppStore } from "../store/useAppStore";
import type { SessionSummary } from "../types";

function buildDraftSession(
  currentSessionId: string,
  userId: string | null,
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
    user_id: userId,
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

function resolveActiveUserId(localUserId: string, userAccountId: string): string {
  return userAccountId.trim() || localUserId;
}

function resolveAccountLabel(userDisplayName: string, userAccountId: string): string {
  return userDisplayName.trim() || userAccountId.trim() || "本机用户";
}

function resolveAccountInitial(label: string): string {
  return (label.trim().slice(0, 1) || "U").toUpperCase();
}

export function AppShell() {
  const navigate = useNavigate();
  const location = useLocation();
  const queryClient = useQueryClient();
  const [accountDialogOpen, setAccountDialogOpen] = useState(false);
  const [draftAccountId, setDraftAccountId] = useState("");
  const [draftDisplayName, setDraftDisplayName] = useState("");
  const [accountError, setAccountError] = useState("");
  const {
    currentWorkspaceId,
    currentSessionId,
    localUserId,
    userAccountId,
    userDisplayName,
    memoryEnabled,
    messagesBySession,
    setCurrentWorkspaceId,
    setCurrentSessionId,
    createDraftSession,
    removeSession,
    setUserAccount,
    clearUserAccount,
    setMemoryEnabled,
  } = useAppStore(
    useShallow((state) => ({
      currentWorkspaceId: state.currentWorkspaceId,
      currentSessionId: state.currentSessionId,
      localUserId: state.localUserId,
      userAccountId: state.userAccountId,
      userDisplayName: state.userDisplayName,
      memoryEnabled: state.memoryEnabled,
      messagesBySession: state.messagesBySession,
      setCurrentWorkspaceId: state.setCurrentWorkspaceId,
      setCurrentSessionId: state.setCurrentSessionId,
      createDraftSession: state.createDraftSession,
      removeSession: state.removeSession,
      setUserAccount: state.setUserAccount,
      clearUserAccount: state.clearUserAccount,
      setMemoryEnabled: state.setMemoryEnabled,
    })),
  );
  const activeUserId = resolveActiveUserId(localUserId, userAccountId);
  const accountLabel = resolveAccountLabel(userDisplayName, userAccountId);
  const accountInitial = resolveAccountInitial(accountLabel);

  const workspacesQuery = useQuery({
    queryKey: queryKeys.workspaces,
    queryFn: listWorkspaces,
  });
  const sessionsQuery = useQuery({
    queryKey: queryKeys.sessions(
      currentWorkspaceId,
      memoryEnabled ? activeUserId : "",
    ),
    queryFn: () =>
      listSessions(
        currentWorkspaceId || undefined,
        memoryEnabled ? activeUserId : undefined,
      ),
  });

  const deleteSessionMutation = useMutation({
    mutationFn: deleteSession,
    onSuccess: (_, sessionId) => {
      removeSession(sessionId);
      queryClient.invalidateQueries({
        queryKey: queryKeys.sessions(
          currentWorkspaceId,
          memoryEnabled ? activeUserId : "",
        ),
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
    const draftSession = buildDraftSession(
      currentSessionId,
      memoryEnabled ? activeUserId : null,
      messagesBySession,
    );
    if (!draftSession) {
      return remoteSessions;
    }
    if (remoteSessions.some((item) => item.session_id === draftSession.session_id)) {
      return remoteSessions;
    }
    return [draftSession, ...remoteSessions];
  }, [
    currentSessionId,
    activeUserId,
    memoryEnabled,
    messagesBySession,
    sessionsQuery.data?.sessions,
  ]);
  const isSpacesRoute = location.pathname.startsWith("/spaces");

  function openAccountDialog() {
    setDraftAccountId(userAccountId || activeUserId);
    setDraftDisplayName(userDisplayName);
    setAccountError("");
    setAccountDialogOpen(true);
  }

  function saveAccountDialog() {
    const normalizedAccountId = draftAccountId.trim();
    if (!normalizedAccountId) {
      setAccountError("账号 ID 不能为空");
      return;
    }
    setUserAccount(normalizedAccountId, draftDisplayName);
    setAccountError("");
    setAccountDialogOpen(false);
    queryClient.invalidateQueries({ queryKey: ["sessions"] });
  }

  function logoutAccount() {
    clearUserAccount();
    setDraftAccountId(localUserId);
    setDraftDisplayName("");
    setAccountError("");
    setAccountDialogOpen(false);
    queryClient.invalidateQueries({ queryKey: ["sessions"] });
  }

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
            <Icon className="sidebar-space-icon" name="database" />
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

        <footer className="sidebar-account">
          <button
            className="sidebar-account-button"
            type="button"
            aria-label="用户登录与记忆设置"
            onClick={openAccountDialog}
          >
            <span className="sidebar-account-avatar">{accountInitial}</span>
            <span className="sidebar-account-copy">
              <strong>{accountLabel}</strong>
              <small>{memoryEnabled ? activeUserId : "记忆关闭"}</small>
            </span>
          </button>
          <button
            className="ghost-button icon-button sidebar-account-settings"
            type="button"
            aria-label="账号设置"
            onClick={openAccountDialog}
          >
            <Icon name="settings" />
          </button>
        </footer>
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
                <Icon className="nav-icon" name="graph" />
                知识图谱
              </NavLink>
              <NavLink
                className={({ isActive }) =>
                  `nav-button ${isActive ? "active" : ""}`.trim()
                }
                to="/chat"
              >
                <Icon className="nav-icon" name="chat" />
                对话
              </NavLink>
              <NavLink
                className={({ isActive }) =>
                  `nav-button ${isActive ? "active" : ""}`.trim()
                }
                to="/discover"
              >
                <Icon className="nav-icon" name="discover" />
                发现
              </NavLink>
            </nav>
          </header>
        ) : null}

        <main className={`content-shell route-${location.pathname.split("/").pop()}`}>
          <Outlet />
        </main>
      </div>

      <Modal
        actions={
          <>
            {userAccountId ? (
              <button className="ghost-button" type="button" onClick={logoutAccount}>
                退出登录
              </button>
            ) : null}
            <button
              className="ghost-button"
              type="button"
              onClick={() => setAccountDialogOpen(false)}
            >
              取消
            </button>
            <button className="primary-button" type="button" onClick={saveAccountDialog}>
              保存
            </button>
          </>
        }
        description="同一个账号 ID 会复用会话历史、跨会话检索和用户记忆配置。"
        open={accountDialogOpen}
        title="用户登录"
        onClose={() => setAccountDialogOpen(false)}
      >
        <div className="account-settings-form">
          <label className="field">
            <span className="field-label">账号 ID</span>
            <input
              className="input"
              value={draftAccountId}
              onChange={(event) => setDraftAccountId(event.target.value)}
              placeholder="例如 hang-yi"
            />
          </label>
          <label className="field">
            <span className="field-label">显示名称</span>
            <input
              className="input"
              value={draftDisplayName}
              onChange={(event) => setDraftDisplayName(event.target.value)}
              placeholder="例如 hang yi"
            />
          </label>
          <div className="account-memory-row">
            <span>
              <strong>记忆系统</strong>
              <small>开启后，请求会携带当前账号 ID 作为 user_id。</small>
            </span>
            <button
              className={`web-search-switch ${memoryEnabled ? "active" : ""}`}
              type="button"
              aria-label={memoryEnabled ? "关闭记忆" : "开启记忆"}
              aria-pressed={memoryEnabled}
              onClick={() => setMemoryEnabled(!memoryEnabled)}
            >
              {memoryEnabled ? "关闭" : "开启"}
            </button>
          </div>
          <div className="account-id-preview">
            当前记忆账号：{memoryEnabled ? activeUserId : "记忆关闭"}
          </div>
          {accountError ? <div className="error-state inline-error">{accountError}</div> : null}
        </div>
      </Modal>
    </div>
  );
}
