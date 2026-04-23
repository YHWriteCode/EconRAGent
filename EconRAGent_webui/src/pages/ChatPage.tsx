import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useShallow } from "zustand/react/shallow";

import { getSessionMessages, streamChat, uploadFile } from "../lib/api";
import { formatTime } from "../lib/format";
import { queryKeys } from "../lib/queryKeys";
import { createSessionId, useAppStore } from "../store/useAppStore";
import type { ChatMessage, UploadRecord, WebSearchMode } from "../types";

function normalizeServerMessage(message: Record<string, unknown>): ChatMessage {
  return {
    clientId:
      typeof message.clientId === "string" ? message.clientId : createSessionId(),
    role:
      message.role === "assistant" || message.role === "user"
        ? message.role
        : "assistant",
    content: typeof message.content === "string" ? message.content : "",
    timestamp:
      typeof message.timestamp === "string"
        ? message.timestamp
        : new Date().toISOString(),
    metadata:
      message.metadata && typeof message.metadata === "object"
        ? (message.metadata as Record<string, unknown>)
        : {},
    session_id:
      typeof message.session_id === "string" ? message.session_id : undefined,
    user_id: typeof message.user_id === "string" ? message.user_id : null,
  };
}

function renderAttachmentLabel(upload: UploadRecord) {
  return `${upload.filename} · ${upload.kind}`;
}

function resolveWebSearchOverride(mode: WebSearchMode): boolean | undefined {
  if (mode === "on") {
    return true;
  }
  if (mode === "off") {
    return false;
  }
  return undefined;
}

function resolveWebSearchLabel(mode: WebSearchMode) {
  if (mode === "on") {
    return "开启";
  }
  if (mode === "off") {
    return "关闭";
  }
  return "自动";
}

function readObject(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object"
    ? (value as Record<string, unknown>)
    : null;
}

export function ChatPage() {
  const queryClient = useQueryClient();
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const [input, setInput] = useState("");
  const [statusText, setStatusText] = useState("");
  const [errorText, setErrorText] = useState("");
  const [menuOpen, setMenuOpen] = useState(false);
  const [isSending, setIsSending] = useState(false);
  const [isUploading, setIsUploading] = useState(false);
  const {
    currentSessionId,
    currentWorkspaceId,
    queryMode,
    webSearchMode,
    pendingAttachments,
    messagesBySession,
    setCurrentSessionId,
    createDraftSession,
    appendMessages,
    updateMessage,
    setSessionMessages,
    addPendingAttachment,
    removePendingAttachment,
    clearPendingAttachments,
    setQueryMode,
    setWebSearchMode,
  } = useAppStore(
    useShallow((state) => ({
      currentSessionId: state.currentSessionId,
      currentWorkspaceId: state.currentWorkspaceId,
      queryMode: state.queryMode,
      webSearchMode: state.webSearchMode,
      pendingAttachments: state.pendingAttachments,
      messagesBySession: state.messagesBySession,
      setCurrentSessionId: state.setCurrentSessionId,
      createDraftSession: state.createDraftSession,
      appendMessages: state.appendMessages,
      updateMessage: state.updateMessage,
      setSessionMessages: state.setSessionMessages,
      addPendingAttachment: state.addPendingAttachment,
      removePendingAttachment: state.removePendingAttachment,
      clearPendingAttachments: state.clearPendingAttachments,
      setQueryMode: state.setQueryMode,
      setWebSearchMode: state.setWebSearchMode,
    })),
  );

  useEffect(() => {
    if (!currentSessionId) {
      createDraftSession();
    }
  }, [createDraftSession, currentSessionId]);

  const sessionMessages = messagesBySession[currentSessionId] ?? [];
  const isLanding = sessionMessages.length === 0;

  const messagesQuery = useQuery({
    queryKey: queryKeys.sessionMessages(currentSessionId),
    queryFn: () => getSessionMessages(currentSessionId),
    enabled: Boolean(currentSessionId),
  });

  useEffect(() => {
    if (!currentSessionId || isSending || !messagesQuery.data) {
      return;
    }
    const nextMessages = (messagesQuery.data.messages ?? []).map((message) =>
      normalizeServerMessage(message),
    );
    if (!nextMessages.length && sessionMessages.length) {
      return;
    }
    setSessionMessages(currentSessionId, nextMessages);
  }, [
    currentSessionId,
    isSending,
    messagesQuery.data,
    sessionMessages.length,
    setSessionMessages,
  ]);

  useEffect(() => {
    const textarea = textareaRef.current;
    if (!textarea) {
      return;
    }
    textarea.style.height = "auto";
    textarea.style.height = `${Math.min(textarea.scrollHeight, 96)}px`;
  }, [input]);

  async function handleUpload(file: File | null) {
    if (!file) {
      return;
    }
    setIsUploading(true);
    setErrorText("");
    try {
      const payload = await uploadFile(file);
      addPendingAttachment(payload.upload);
      setStatusText(`${file.name} 已上传，可用于本轮问答。`);
      setMenuOpen(false);
    } catch (error) {
      setErrorText(error instanceof Error ? error.message : "上传失败");
    } finally {
      setIsUploading(false);
      if (fileInputRef.current) {
        fileInputRef.current.value = "";
      }
    }
  }

  async function handleSend(textOverride?: string) {
    const text = (textOverride ?? input).trim();
    if (!text || isSending) {
      return;
    }
    const sessionId = currentSessionId || createDraftSession();
    setCurrentSessionId(sessionId);
    setIsSending(true);
    setErrorText("");
    setStatusText("正在流式生成回答...");

    const userMessage: ChatMessage = {
      clientId: createSessionId(),
      role: "user",
      content: text,
      timestamp: new Date().toISOString(),
      metadata: {
        workspace: currentWorkspaceId || null,
        attachments: pendingAttachments.map((upload) => ({
          upload_id: upload.upload_id,
          filename: upload.filename,
          kind: upload.kind,
        })),
      },
    };
    const assistantMessageId = createSessionId();
    const assistantMessage: ChatMessage = {
      clientId: assistantMessageId,
      role: "assistant",
      content: "",
      timestamp: new Date().toISOString(),
      metadata: {
        status: "streaming",
      },
    };
    appendMessages(sessionId, [userMessage, assistantMessage]);
    const attachmentIds = pendingAttachments.map((upload) => upload.upload_id);
    clearPendingAttachments();
    setInput("");

    try {
      const finalPayload = await streamChat(
        {
          query: text,
          session_id: sessionId,
          workspace: currentWorkspaceId || undefined,
          query_mode: queryMode,
          force_web_search: resolveWebSearchOverride(webSearchMode),
          attachment_ids: attachmentIds,
        },
        {
          onEvent: (event) => {
            if (event.type === "delta" && typeof event.content === "string") {
              updateMessage(sessionId, assistantMessageId, (message) => ({
                ...message,
                content: `${message.content}${event.content ?? ""}`,
              }));
            }
            if (event.type === "meta" && event.metadata) {
              updateMessage(sessionId, assistantMessageId, (message) => ({
                ...message,
                metadata: {
                  ...message.metadata,
                  response_metadata: event.metadata,
                },
              }));
            }
            if (event.type === "tool_result" && event.tool_call) {
              updateMessage(sessionId, assistantMessageId, (message) => {
                const nextToolCalls = Array.isArray(message.metadata.tool_calls)
                  ? [...(message.metadata.tool_calls as Array<Record<string, unknown>>)]
                  : [];
                nextToolCalls.push(event.tool_call);
                return {
                  ...message,
                  metadata: {
                    ...message.metadata,
                    tool_calls: nextToolCalls,
                  },
                };
              });
            }
          },
        },
      );

      if (finalPayload) {
        updateMessage(sessionId, assistantMessageId, (message) => ({
          ...message,
          content: finalPayload.answer || message.content,
          metadata: {
            ...message.metadata,
            response_metadata: finalPayload.metadata,
            tool_calls: finalPayload.tool_calls,
            path_explanation: finalPayload.path_explanation,
            route: finalPayload.route,
          },
        }));
        if (finalPayload.metadata.unsupported_multimodal) {
          setStatusText("图片已进入上传链路，但当前后端未启用视觉理解模型。");
        } else {
          setStatusText("");
        }
      }
      queryClient.invalidateQueries({
        queryKey: queryKeys.sessions(currentWorkspaceId),
      });
      queryClient.invalidateQueries({
        queryKey: queryKeys.sessionMessages(sessionId),
      });
    } catch (error) {
      const message = error instanceof Error ? error.message : "发送失败";
      updateMessage(sessionId, assistantMessageId, (draft) => ({
        ...draft,
        content: message,
        metadata: {
          ...draft.metadata,
          error: message,
        },
      }));
      setErrorText(message);
      setStatusText("");
    } finally {
      setIsSending(false);
    }
  }

  const webSearchEnabled = webSearchMode === "on";

  const attachmentStrip = pendingAttachments.length ? (
    <div className="composer-attachments">
      {pendingAttachments.map((upload) => (
        <div className="attachment-card" key={upload.upload_id}>
          <span>{renderAttachmentLabel(upload)}</span>
          <button
            className="ghost-button icon-button"
            type="button"
            onClick={() => removePendingAttachment(upload.upload_id)}
          >
            移除
          </button>
        </div>
      ))}
    </div>
  ) : null;

  const composerMenu = menuOpen ? (
    <div
      className={`upload-menu minimal-upload-menu composer-popover ${
        isLanding ? "composer-popover-down" : "composer-popover-up"
      }`}
    >
      <div className="upload-menu-options">
        <button
          className="upload-menu-item"
          type="button"
          onClick={() => fileInputRef.current?.click()}
        >
          <span className="menu-item-icon" aria-hidden="true">
            ↑
          </span>
          <span>
            <strong>上传图片和文件</strong>
            <small>PNG, JPG, PDF, TXT, DOCX</small>
          </span>
        </button>
        <div className="upload-menu-item upload-menu-switch">
          <span className="menu-item-icon" aria-hidden="true">
            ◎
          </span>
          <span>
            <strong>联网搜索</strong>
            <small>手动控制联网搜索</small>
          </span>
          <button
            className={`web-search-switch ${webSearchEnabled ? "active" : ""}`}
            type="button"
            aria-pressed={webSearchEnabled}
            onClick={() => setWebSearchMode(webSearchEnabled ? "off" : "on")}
          >
            {webSearchEnabled ? "关闭" : "开启"}
          </button>
        </div>
      </div>
    </div>
  ) : null;

  const composerBody = (
    <>
      {attachmentStrip}
      <div className="composer-frame">
        <div className="composer-surface">
          <textarea
            className={`composer-textarea ${isLanding ? "composer-textarea-home" : ""}`}
            onChange={(event) => setInput(event.target.value)}
            placeholder="有什么问题尽管问我..."
            ref={textareaRef}
            rows={1}
            value={input}
          />
          <div className="composer-toolbar">
            <div className="composer-toolbar-left">
              <button
                className="ghost-button composer-plus"
                type="button"
                aria-label="打开上传和联网搜索菜单"
                onClick={() => setMenuOpen((value) => !value)}
              >
                +
              </button>
              <button className="workspace-chip" type="button">
                <span className="workspace-chip-icon" aria-hidden="true">
                  ◉
                </span>
                {currentWorkspaceId || "所有知识图谱"}
              </button>
            </div>
            <div className="composer-toolbar-right">
              <span className="composer-meta-note">{`联网 ${resolveWebSearchLabel(webSearchMode)}`}</span>
              <button
                className="composer-send-button"
                aria-label={isSending ? "发送中" : "发送"}
                disabled={isSending || isUploading}
                type="button"
                onClick={() => handleSend()}
              >
                {isSending ? "..." : ">"}
              </button>
            </div>
          </div>
        </div>
        {composerMenu}
      </div>
      <div className="mode-row composer-mode-row" aria-label="检索模式">
        {(["naive", "local", "global", "hybrid", "mix"] as const).map((mode) => (
          <button
            className={`mode-button ${queryMode === mode ? "active" : ""}`}
            key={mode}
            type="button"
            onClick={() => setQueryMode(mode)}
          >
            {mode}
          </button>
        ))}
      </div>
      <div className="composer-status">
        {statusText ? <span className="muted">{statusText}</span> : null}
        {errorText ? <span className="error-inline">{errorText}</span> : null}
      </div>
    </>
  );

  return (
    <div className={`chat-layout chat-layout-single ${isLanding ? "chat-layout-home" : ""}`}>
      <section className={`panel chat-panel ${isLanding ? "chat-panel-home" : ""}`}>
        {isLanding ? (
          <div className="chat-home-shell">
            <div className="chat-home-copy">
              <h1>晚上好，hang yi</h1>
              <p>我可以基于知识图谱为你答疑解惑、分析研究、提供洞见。</p>
            </div>

            <div className="panel home-composer-panel">{composerBody}</div>
          </div>
        ) : (
          <>
            <div className="message-feed">
              {sessionMessages.map((message) => {
                const cardMetadata = readObject(message.metadata.response_metadata);
                const toolCalls = Array.isArray(message.metadata.tool_calls)
                  ? message.metadata.tool_calls
                  : [];
                const attachments = Array.isArray(message.metadata.attachments)
                  ? message.metadata.attachments
                  : [];

                return (
                  <article className={`message ${message.role}`} key={message.clientId}>
                    <div className="message-meta">{formatTime(message.timestamp)}</div>
                    <div className="message-bubble">{message.content || "..."}</div>
                    {attachments.length ? (
                      <div className="attachment-list">
                        {attachments.map((attachment) => {
                          const typed = attachment as Record<string, unknown>;
                          return (
                            <div
                              className="attachment-card"
                              key={String(typed.upload_id)}
                            >
                              {String(typed.filename || typed.upload_id)}
                            </div>
                          );
                        })}
                      </div>
                    ) : null}
                    {message.role === "assistant" && cardMetadata ? (
                      <div className="message-badges">
                        {cardMetadata.effective_query_mode ? (
                          <span className="tag">
                            检索模式 {String(cardMetadata.effective_query_mode)}
                          </span>
                        ) : null}
                        {cardMetadata.web_search_forced === true ? (
                          <span className="tag">联网强制开启</span>
                        ) : null}
                        {cardMetadata.web_search_forced === false ? (
                          <span className="tag">联网强制关闭</span>
                        ) : null}
                        {toolCalls.length ? (
                          <span className="tag">工具调用 {toolCalls.length} 次</span>
                        ) : null}
                      </div>
                    ) : null}
                  </article>
                );
              })}
            </div>

            <footer className="composer">{composerBody}</footer>
          </>
        )}

        <input
          className="hidden"
          onChange={(event) => void handleUpload(event.target.files?.[0] ?? null)}
          ref={fileInputRef}
          type="file"
        />
      </section>
    </div>
  );
}
