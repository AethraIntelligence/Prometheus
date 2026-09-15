/**
 * The + under the field, and the tray under the composer that says where the work happens.
 *
 * The + is a menu rather than a button because files, a document and whatever
 * comes next belong in the same place, and a row of single-purpose buttons
 * under the field is what that turns into otherwise. For now it holds one
 * thing: the folder.
 *
 * A folder somebody chose is shown in a tray under the composer, so a task
 * pointed at another folder says so at a glance. A task's own folder is what
 * every task has, and a tray announcing the default on every screen would be
 * noise; it appears only once the + has been used to choose something else.
 *
 * The folder is chosen here and kept by the core: this reports a path, and
 * whether a folder is acceptable is decided by the runtime, which says so where
 * the page shows its problems.
 */

import { useEffect, useRef, useState, type RefObject } from "react";

import { chooseFolders } from "../../../shared/api";
import { CheckIcon, ChevronDown, ChevronRight, FolderIcon, PlusIcon } from "../../../shared/ui";

/** The last part of a path, which is what a person calls a folder. */
export function folderLabel(path: string): string {
  return path.split(/[\\/]/).filter(Boolean).pop() ?? path;
}

interface FolderProps {
  /** The folder the work is in now, as a full path. Empty: a folder of the task's own, not made yet. */
  folder: string;
  /** Where a task's own folder is made. Shown so "its own folder" says where. */
  fileRoot?: string;
  /** Folders saved in Settings -> Workspaces. */
  saved: string[];
  onChoose: (folder: string) => void | Promise<void>;
  disabled?: boolean;
}

/** A task's own folder is under the workspace's files and is not one somebody saved. */
function isOwn({ folder, fileRoot = "", saved }: FolderProps): boolean {
  return !folder || (!!fileRoot && folder.startsWith(fileRoot) && !saved.includes(folder));
}

/** Closed by a click elsewhere or by Escape, the way every menu people know closes. */
function useDismiss(open: boolean, holder: RefObject<HTMLElement>, close: () => void) {
  useEffect(() => {
    if (!open) return;
    const away = (event: MouseEvent) => {
      if (!holder.current?.contains(event.target as Node)) close();
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key === "Escape") close();
    };
    document.addEventListener("mousedown", away);
    document.addEventListener("keydown", escape);
    return () => {
      document.removeEventListener("mousedown", away);
      document.removeEventListener("keydown", escape);
    };
  }, [open, holder, close]);
}

function FolderOptions(props: FolderProps & { onDone: () => void }) {
  const { folder, fileRoot = "", saved, onChoose, onDone } = props;
  const own = isOwn(props);

  const choose = async (next: string) => {
    onDone();
    if (next !== folder) await onChoose(next);
  };

  const browse = async () => {
    const chosen = await chooseFolders({ title: "Folder to work in" });
    const path =
      chosen === null ? window.prompt("The full path of the folder:")?.trim() : chosen[0];
    if (path) await choose(path);
    else onDone();
  };

  return (
    <div className="plus-menu" role="menu" aria-label="Work in a folder">
      <p className="plus-head">Work in a folder</p>
      <button type="button" role="menuitemradio" aria-checked={own} onClick={() => void choose("")}>
        <FolderIcon />
        <span className="plus-label">Its own folder</span>
        <span className="plus-hint" title={fileRoot}>
          {fileRoot ? `in ${folderLabel(fileRoot)}` : "made for this task"}
        </span>
        {own && <CheckIcon className="plus-go" />}
      </button>
      {saved.map((item) => (
        <button
          type="button"
          role="menuitemradio"
          aria-checked={item === folder}
          key={item}
          title={item}
          onClick={() => void choose(item)}
        >
          <FolderIcon />
          <span className="plus-label">{folderLabel(item)}</span>
          <span className="plus-hint">{item}</span>
          {item === folder && <CheckIcon className="plus-go" />}
        </button>
      ))}
      {folder && !own && !saved.includes(folder) && (
        <button type="button" role="menuitemradio" aria-checked title={folder}>
          <FolderIcon />
          <span className="plus-label">{folderLabel(folder)}</span>
          <span className="plus-hint">{folder}</span>
          <CheckIcon className="plus-go" />
        </button>
      )}
      <button type="button" role="menuitem" onClick={() => void browse()}>
        <PlusIcon />
        <span className="plus-label">Choose a folder…</span>
      </button>
    </div>
  );
}

type View = "root" | "folder";

/** The + button, inside the composer. */
export function ComposerMenu(props: FolderProps) {
  const [open, setOpen] = useState<View | null>(null);
  const holder = useRef<HTMLDivElement>(null);
  const close = () => setOpen(null);
  useDismiss(open !== null, holder, close);

  return (
    <div className="plus" ref={holder}>
      <button
        type="button"
        className="plus-btn"
        aria-label="Add to the request"
        aria-expanded={open !== null}
        disabled={props.disabled}
        onClick={() => setOpen(open ? null : "root")}
      >
        <PlusIcon />
      </button>

      {open === "root" && (
        <div className="plus-menu" role="menu" aria-label="Add to the request">
          <p className="plus-head">Add</p>
          <button type="button" role="menuitem" onClick={() => setOpen("folder")}>
            <FolderIcon />
            <span className="plus-label">Work in a folder</span>
            <span className="plus-hint">
              {isOwn(props) ? "Its own folder" : folderLabel(props.folder)}
            </span>
            <ChevronRight className="plus-go" />
          </button>
        </div>
      )}
      {open === "folder" && <FolderOptions {...props} onDone={close} />}
    </div>
  );
}

/** The tray under the composer: a folder somebody chose, and a way to change it. Nothing for the default. */
export function FolderTray(props: FolderProps) {
  const [open, setOpen] = useState(false);
  const holder = useRef<HTMLDivElement>(null);
  const close = () => setOpen(false);
  useDismiss(open, holder, close);

  if (isOwn(props)) return null;
  const label = folderLabel(props.folder);

  return (
    <div className="composer-tray" ref={holder}>
      <button
        type="button"
        className="tray-folder"
        title={props.folder}
        aria-label={`Folder: ${label}`}
        aria-expanded={open}
        disabled={props.disabled}
        onClick={() => setOpen(!open)}
      >
        <FolderIcon />
        <b>{label}</b>
        <ChevronDown />
      </button>
      {open && <FolderOptions {...props} onDone={close} />}
    </div>
  );
}
