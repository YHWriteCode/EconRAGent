import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { useShallow } from "zustand/react/shallow";

import { Modal } from "../components/Modal";
import {
  createWorkspace,
  createWorkspaceImport,
  deleteWorkspace,
  listWorkspaces,
  updateWorkspace,
  uploadFile,
} from "../lib/api";
import { formatTime } from "../lib/format";
import { queryKeys } from "../lib/queryKeys";
import { useAppStore } from "../store/useAppStore";
import type { WorkspaceSummary } from "../types";

type DialogState =
  | { kind: "create" }
  | { kind: "rename"; workspace: WorkspaceSummary }
  | { kind: "text"; workspace: WorkspaceSummary }
  | { kind: "url"; workspace: WorkspaceSummary }
  | { kind: "upload"; workspace: WorkspaceSummary }
  | null;

const SPACE_THEMES = [
  { icon: "▟", tone: "green" },
  { icon: "▥", tone: "sage" },
  { icon: "◎", tone: "violet" },
  { icon: "▰", tone: "amber" },
  { icon: "●", tone: "blue" },
  { icon: "▤", tone: "indigo" },
  { icon: "▣", tone: "orange" },
  { icon: "◇", tone: "teal" },
  { icon: "♨", tone: "coral" },
  { icon: "◒", tone: "olive" },
] as const;

export function SpacesPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { currentWorkspaceId, setCurrentWorkspaceId } = useAppStore(
    useShallow((state) => ({
      currentWorkspaceId: state.currentWorkspaceId,
      setCurrentWorkspaceId: state.setCurrentWorkspaceId,
    })),
  );
  const [dialog, setDialog] = useState<DialogState>(null);
  const [dialogInput, setDialogInput] = useState("");
  const [dialogSecondaryInput, setDialogSecondaryInput] = useState("");
  const [dialogFile, setDialogFile] = useState<File | null>(null);
  const [formError, setFormError] = useState("");
  const [openMenuWorkspaceId, setOpenMenuWorkspaceId] = useState("");
  const [activeImport, setActiveImport] = useState<{
    trackId: string;
    workspaceId: string;
  } | null>(null);

  const workspacesQuery = useQuery({
    queryKey: queryKeys.workspaces,
    queryFn: listWorkspaces,
  });

  const createMutation = useMutation({
    mutationFn: createWorkspace,
    onSuccess: (workspace) => {
      setCurrentWorkspaceId(workspace.workspace_id);
      queryClient.invalidateQueries({ queryKey: queryKeys.workspaces });
      resetDialog();
    },
  });

  const renameMutation = useMutation({
    mutationFn: ({
      workspaceId,
      displayName,
      description,
    }: {
      workspaceId: string;
      displayName: string;
      description?: string;
    }) =>
      updateWorkspace(workspaceId, {
        display_name: displayName,
        description,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.workspaces });
      resetDialog();
    },
  });

  const deleteMutation = useMutation({
    mutationFn: deleteWorkspace,
    onSuccess: (_, workspaceId) => {
      queryClient.invalidateQueries({ queryKey: queryKeys.workspaces });
      if (currentWorkspaceId === workspaceId) {
        setCurrentWorkspaceId("");
      }
      setOpenMenuWorkspaceId("");
    },
  });

  const importMutation = useMutation({
    mutationFn: async (payload: {
      workspaceId: string;
      kind: "text" | "url" | "upload";
      value: string | File;
    }) => {
      if (payload.kind === "upload") {
        const uploadPayload = await uploadFile(payload.value as File);
        return createWorkspaceImport(payload.workspaceId, {
          kind: "upload",
          upload_id: uploadPayload.upload.upload_id,
        });
      }
      if (payload.kind === "text") {
        return createWorkspaceImport(payload.workspaceId, {
          kind: "text",
          text: String(payload.value),
        });
      }
      return createWorkspaceImport(payload.workspaceId, {
        kind: "url",
        url: String(payload.value),
      });
    },
    onSuccess: (payload) => {
      setActiveImport({
        trackId: payload.track_id,
        workspaceId: payload.workspace_id,
      });
      queryClient.invalidateQueries({ queryKey: queryKeys.workspaces });
      resetDialog();
    },
  });

  const workspaces = workspacesQuery.data?.workspaces ?? [];
  const totalDocuments = useMemo(
    () => workspaces.reduce((sum, workspace) => sum + workspace.document_count, 0),
    [workspaces],
  );

  const isBusy =
    createMutation.isPending ||
    renameMutation.isPending ||
    deleteMutation.isPending ||
    importMutation.isPending;

  function resetDialog() {
    setDialog(null);
    setDialogInput("");
    setDialogSecondaryInput("");
    setDialogFile(null);
    setFormError("");
  }

  function openCreateDialog() {
    setDialog({ kind: "create" });
    setDialogInput("");
    setDialogSecondaryInput("");
    setFormError("");
  }

  async function submitDialog() {
    if (!dialog) {
      return;
    }
    setFormError("");
    try {
      if (dialog.kind === "create") {
        if (!dialogInput.trim()) {
          throw new Error("新建空间名称不能为空");
        }
        await createMutation.mutateAsync({
          display_name: dialogInput.trim(),
          description: dialogSecondaryInput.trim() || undefined,
        });
        return;
      }
      if (dialog.kind === "rename") {
        if (!dialogInput.trim()) {
          throw new Error("显示名称不能为空");
        }
        await renameMutation.mutateAsync({
          workspaceId: dialog.workspace.workspace_id,
          displayName: dialogInput.trim(),
          description: dialogSecondaryInput.trim() || undefined,
        });
        return;
      }
      if (dialog.kind === "text") {
        if (!dialogInput.trim()) {
          throw new Error("请输入要导入的文本");
        }
        await importMutation.mutateAsync({
          workspaceId: dialog.workspace.workspace_id,
          kind: "text",
          value: dialogInput.trim(),
        });
        return;
      }
      if (dialog.kind === "url") {
        if (!dialogInput.trim()) {
          throw new Error("请输入 URL");
        }
        await importMutation.mutateAsync({
          workspaceId: dialog.workspace.workspace_id,
          kind: "url",
          value: dialogInput.trim(),
        });
        return;
      }
      if (!dialogFile) {
        throw new Error("请选择要上传的文件");
      }
      await importMutation.mutateAsync({
        workspaceId: dialog.workspace.workspace_id,
        kind: "upload",
        value: dialogFile,
      });
    } catch (error) {
      setFormError(error instanceof Error ? error.message : "提交失败");
    }
  }

  async function handleDelete(workspace: WorkspaceSummary) {
    if (!window.confirm("删除空间会清空该 workspace 的数据，确认继续？")) {
      return;
    }
    await deleteMutation.mutateAsync(workspace.workspace_id);
  }

  return (
    <section className="spaces-page">
      <header className="spaces-page-topbar">
        <button
          className="spaces-close-button"
          type="button"
          aria-label="退出空间页面"
          onClick={() => navigate("/chat")}
        >
          ×
        </button>
        <button
          className="spaces-create-button"
          type="button"
          aria-label="新建空间"
          onClick={openCreateDialog}
        >
          <span>+</span>
          <span>新空间</span>
        </button>
      </header>

      <div className="spaces-page-title">
        <h1>空间</h1>
        <p>
          管理您的知识空间与数据库
          <span className="spaces-info-dot" aria-hidden="true">i</span>
        </p>
      </div>

      {activeImport ? (
        <div className="spaces-import-note">
          最近导入任务 {activeImport.trackId} 已提交到 {activeImport.workspaceId}
        </div>
      ) : null}

      {formError ? <div className="error-state inline-error">{formError}</div> : null}

      {workspacesQuery.isLoading ? (
        <div className="empty-state">正在加载空间...</div>
      ) : workspaces.length ? (
        <div className="spaces-card-grid">
          {workspaces.map((workspace, index) => {
            const theme = SPACE_THEMES[index % SPACE_THEMES.length]!;
            const menuOpen = openMenuWorkspaceId === workspace.workspace_id;
            return (
              <article className="database-card" key={workspace.workspace_id}>
                <button
                  className="database-card-menu-trigger"
                  type="button"
                  aria-label={`${workspace.display_name} 操作菜单`}
                  onClick={() =>
                    setOpenMenuWorkspaceId((current) =>
                      current === workspace.workspace_id ? "" : workspace.workspace_id,
                    )
                  }
                >
                  ⋮
                </button>

                {menuOpen ? (
                  <div className="database-card-menu">
                    <button
                      type="button"
                      onClick={() => {
                        setCurrentWorkspaceId(workspace.workspace_id);
                        navigate(`/graph?workspace=${encodeURIComponent(workspace.workspace_id)}`);
                      }}
                    >
                      <span aria-hidden="true">▣</span>
                      查看详情
                    </button>
                    <button
                      type="button"
                      onClick={() => {
                        setDialog({ kind: "rename", workspace });
                        setDialogInput(workspace.display_name);
                        setDialogSecondaryInput(workspace.description || "");
                        setOpenMenuWorkspaceId("");
                      }}
                    >
                      <span aria-hidden="true">✎</span>
                      重命名
                    </button>
                    <button
                      type="button"
                      onClick={() => {
                        setDialog({ kind: "upload", workspace });
                        setDialogFile(null);
                        setOpenMenuWorkspaceId("");
                      }}
                    >
                      <span aria-hidden="true">↥</span>
                      导入文件
                    </button>
                    <button
                      className="database-danger-action"
                      type="button"
                      onClick={() => void handleDelete(workspace)}
                    >
                      <span aria-hidden="true">⌫</span>
                      删除
                    </button>
                  </div>
                ) : null}

                <div className={`database-card-icon database-card-icon-${theme.tone}`}>
                  {theme.icon}
                </div>

                <div className="database-card-copy">
                  <h2>{workspace.display_name}</h2>
                  <p>{workspace.description || "暂无描述，等待导入研究资料"}</p>
                </div>

                <div className="database-card-meta">
                  <span>文档 {workspace.document_count.toLocaleString()}</span>
                  <span>实体 {workspace.node_count.toLocaleString()}</span>
                </div>
                <div className="database-card-updated">
                  更新于 {formatTime(workspace.last_updated_at || workspace.updated_at)}
                </div>
              </article>
            );
          })}
        </div>
      ) : (
        <div className="spaces-empty-state">
          <strong>还没有空间</strong>
          <p>点击右上角「+ 新空间」创建第一个知识库。</p>
        </div>
      )}

      <div className="spaces-total-count">
        共 {workspaces.length} 个空间 · {totalDocuments.toLocaleString()} 篇文档
      </div>

      <Modal
        actions={
          <>
            <button className="ghost-button" type="button" onClick={resetDialog}>
              取消
            </button>
            <button
              className="primary-button"
              disabled={isBusy}
              type="button"
              onClick={() => void submitDialog()}
            >
              提交
            </button>
          </>
        }
        description={
          dialog?.kind === "create"
            ? "创建新的知识库空间，后续可通过卡片菜单导入文件、URL 或文本。"
            : dialog?.kind === "rename"
              ? "仅更新显示名称和描述，不修改底层 workspace_id。"
              : dialog?.kind === "text"
                ? "直接导入文本内容。"
                : dialog?.kind === "url"
                  ? "抓取网页内容后写入当前空间。"
                  : dialog?.kind === "upload"
                    ? "先上传文件，再触发导入。"
                    : undefined
        }
        open={Boolean(dialog)}
        title={
          dialog?.kind === "create"
            ? "新建知识库空间"
            : dialog?.kind === "rename"
              ? `重命名 ${dialog.workspace.display_name}`
              : dialog?.kind === "text"
                ? `导入文本 · ${dialog.workspace.display_name}`
                : dialog?.kind === "url"
                  ? `导入 URL · ${dialog.workspace.display_name}`
                  : dialog?.kind === "upload"
                    ? `导入文件 · ${dialog.workspace.display_name}`
                    : ""
        }
        onClose={resetDialog}
      >
        {dialog?.kind === "create" || dialog?.kind === "rename" ? (
          <>
            <input
              className="input"
              placeholder="显示名称"
              value={dialogInput}
              onChange={(event) => setDialogInput(event.target.value)}
            />
            <input
              className="input"
              placeholder="描述（可选）"
              value={dialogSecondaryInput}
              onChange={(event) => setDialogSecondaryInput(event.target.value)}
            />
          </>
        ) : null}
        {dialog?.kind === "text" ? (
          <textarea
            className="textarea"
            value={dialogInput}
            onChange={(event) => setDialogInput(event.target.value)}
            placeholder="输入要导入的文本"
          />
        ) : null}
        {dialog?.kind === "url" ? (
          <input
            className="input"
            value={dialogInput}
            onChange={(event) => setDialogInput(event.target.value)}
            placeholder="https://example.com/article"
          />
        ) : null}
        {dialog?.kind === "upload" ? (
          <input
            className="input"
            type="file"
            onChange={(event) => setDialogFile(event.target.files?.[0] ?? null)}
          />
        ) : null}
        {formError ? <div className="error-state inline-error">{formError}</div> : null}
      </Modal>
    </section>
  );
}
