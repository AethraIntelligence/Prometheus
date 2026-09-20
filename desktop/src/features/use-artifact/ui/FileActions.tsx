/**
 * What a person can do with a file the work produced, behind one chevron.
 *
 * There used to be a single "Open" button, which answered one of the things
 * people actually want and hid the rest. A menu answers the others - where the
 * file is, its text, its path - and answers only the ones this file and this
 * surface can: a PDF has no text to put on the clipboard, and a page opened in
 * a browser has no way to reach a path at all. An action that cannot work is
 * not offered rather than offered and refused.
 *
 * The file is not downloaded anywhere. It is already on this machine, in the
 * folder the thread works in, so "save it" is "show me where it is".
 */

import { useEffect, useRef, useState } from "react";

import { kindOf, type Artifact } from "../../../entities/conversation";
import { canOpenFiles, copyText, openFile, parentOf, showInFolder } from "../../../shared/api";
import { BookIcon, ChevronDown, CopyIcon, FolderIcon } from "../../../shared/ui";

interface Props {
  artifact: Artifact;
  /** The file as text, where the surface already read it. Empty for a PDF or an image. */
  text?: string;
}

export function FileActions({ artifact, text = "" }: Props) {
  const [open, setOpen] = useState(false);
  const [said, setSaid] = useState("");
  const menu = useRef<HTMLDivElement>(null);
  const kind = kindOf(artifact);
  const onThisMachine = canOpenFiles() && Boolean(artifact.location) && artifact.exists;
  const copyable = (kind === "markdown" || kind === "text") && Boolean(text);

  useEffect(() => {
    if (!open) return;
    const away = (event: MouseEvent) => {
      if (menu.current && !menu.current.contains(event.target as Node)) setOpen(false);
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", away);
    document.addEventListener("keydown", escape);
    return () => {
      document.removeEventListener("mousedown", away);
      document.removeEventListener("keydown", escape);
    };
  }, [open]);

  // What was copied is said for a moment, because a clipboard gives no sign of
  // its own that anything happened.
  useEffect(() => {
    if (!said) return;
    const timer = window.setTimeout(() => setSaid(""), 1800);
    return () => window.clearTimeout(timer);
  }, [said]);

  const copying = async (what: string, value: string) => {
    setOpen(false);
    setSaid((await copyText(value)) ? `${what} copied` : `${what} could not be copied`);
  };

  return (
    <div className="file-acts" ref={menu}>
      {said && <span className="file-said">{said}</span>}
      <button
        type="button"
        className="btn btn-line file-acts-more"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={`Actions for ${artifact.name}`}
        onClick={() => setOpen((now) => !now)}
      >
        Actions
        <ChevronDown />
      </button>
      {open && (
        <div className="menu" role="menu">
          {onThisMachine && (
            <button
              type="button"
              role="menuitem"
              onClick={() => {
                setOpen(false);
                void openFile(artifact.location);
              }}
            >
              <BookIcon />
              Open
            </button>
          )}
          {onThisMachine && parentOf(artifact.location) && (
            <button
              type="button"
              role="menuitem"
              onClick={() => {
                setOpen(false);
                void showInFolder(artifact.location);
              }}
            >
              <FolderIcon />
              Show in folder
            </button>
          )}
          {copyable && (
            <button
              type="button"
              role="menuitem"
              onClick={() => void copying("Contents", text)}
            >
              <CopyIcon />
              Copy contents
            </button>
          )}
          <button
            type="button"
            role="menuitem"
            onClick={() => void copying("Path", artifact.location || artifact.path)}
          >
            <CopyIcon />
            Copy path
          </button>
        </div>
      )}
    </div>
  );
}
