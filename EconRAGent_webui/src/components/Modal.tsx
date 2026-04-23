import type { PropsWithChildren, ReactNode } from "react";

interface ModalProps extends PropsWithChildren {
  open: boolean;
  title: string;
  description?: string;
  actions?: ReactNode;
  onClose: () => void;
}

export function Modal({
  open,
  title,
  description,
  actions,
  onClose,
  children,
}: ModalProps) {
  if (!open) {
    return null;
  }

  return (
    <div className="modal-backdrop" role="presentation" onClick={onClose}>
      <section
        className="modal-card panel"
        role="dialog"
        aria-modal="true"
        aria-label={title}
        onClick={(event) => event.stopPropagation()}
      >
        <header className="modal-header">
          <div className="page-title">
            <h2>{title}</h2>
            {description ? <p>{description}</p> : null}
          </div>
          <button className="ghost-button" type="button" onClick={onClose}>
            关闭
          </button>
        </header>
        <div className="modal-body">{children}</div>
        {actions ? <footer className="modal-footer">{actions}</footer> : null}
      </section>
    </div>
  );
}
