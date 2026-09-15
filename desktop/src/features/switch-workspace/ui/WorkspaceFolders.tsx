/**
 * Where a workspace's files go, and the folders a thread can be pointed at.
 *
 * Chosen with the system's folder dialog rather than typed: a path typed by
 * hand is a path with a typo in it, and the dialog is where a person already
 * knows how to find their folders. A page with no dialog - opened in a browser
 * rather than in the window - asks for the path instead.
 *
 * Nothing here is checked by the window. A folder that would hand over every
 * file on the disk is refused by the core, and the refusal is shown where the
 * section shows its problems.
 */

import type { Workspace } from "../../../entities/workspace";
import { canOpenFiles, chooseFolders, openFile } from "../../../shared/api";
import { CloseIcon, FolderIcon, PlusIcon } from "../../../shared/ui";

interface Props {
  workspace: Workspace;
  onChange: (change: { file_root?: string; folders?: string[] }) => void | Promise<void>;
  disabled?: boolean;
}

async function pick(title: string, multiple: boolean): Promise<string[]> {
  const chosen = await chooseFolders({ title, multiple });
  if (chosen !== null) return chosen;
  const typed = window.prompt(`${title} - the full path:`)?.trim();
  return typed ? [typed] : [];
}

export function WorkspaceFolders({ workspace, onChange, disabled }: Props) {
  const folders = workspace.folders ?? [];

  const changeRoot = async () => {
    const [chosen] = await pick("Where this workspace keeps its files", false);
    if (chosen) await onChange({ file_root: chosen });
  };

  const addFolders = async () => {
    const chosen = await pick("Folders to work in", true);
    if (chosen.length > 0) await onChange({ folders: [...folders, ...chosen] });
  };

  return (
    <div className="ws-folders">
      <div className="ws-folder root">
        <FolderIcon />
        <span className="ws-folder-text">
          <b>Files</b>
          <span title={workspace.file_root}>{workspace.file_root}</span>
        </span>
        {canOpenFiles() && (
          <button type="button" onClick={() => void openFile(workspace.file_root)}>
            Show
          </button>
        )}
        <button type="button" onClick={() => void changeRoot()} disabled={disabled}>
          Change…
        </button>
      </div>
      <p className="note">Each task gets a folder of its own in here.</p>

      {folders.map((folder) => (
        <div className="ws-folder" key={folder}>
          <FolderIcon />
          <span className="ws-folder-text">
            <b>{folder.split(/[\\/]/).filter(Boolean).pop() ?? folder}</b>
            <span title={folder}>{folder}</span>
          </span>
          <button
            type="button"
            className="icobtn"
            aria-label={`Forget ${folder}`}
            disabled={disabled}
            onClick={() => void onChange({ folders: folders.filter((item) => item !== folder) })}
          >
            <CloseIcon />
          </button>
        </div>
      ))}
      <button
        type="button"
        className="addbtn"
        onClick={() => void addFolders()}
        disabled={disabled}
      >
        <PlusIcon />
        Add folder
      </button>
    </div>
  );
}
