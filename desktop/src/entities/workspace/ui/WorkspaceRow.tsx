import type { ReactNode } from "react";

import type { Workspace } from "../model/types";

/** One workspace, as a line in the list that manages them. */
export function WorkspaceRow({
  workspace,
  actions,
  folders,
}: {
  workspace: Workspace;
  actions?: ReactNode;
  /** Where its files are, drawn by whoever may change them. The path alone otherwise. */
  folders?: ReactNode;
}) {
  return (
    <article className={workspace.active ? "workspace here" : "workspace"}>
      <header>
        <strong>{workspace.name}</strong>
        {workspace.active && <span className="badge">working here</span>}
        {workspace.is_default && <span className="badge quiet">first</span>}
      </header>
      {workspace.description && <p>{workspace.description}</p>}
      {folders ?? <p className="note">{workspace.file_root}</p>}
      {actions}
    </article>
  );
}
