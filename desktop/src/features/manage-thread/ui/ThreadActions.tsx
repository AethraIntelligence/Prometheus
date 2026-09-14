/**
 * The menu behind a thread's "…": rename it, or delete it.
 *
 * Renaming happens in the row itself - the title is already where the eye is,
 * and a dialog for one line of text is a detour. Deleting asks first, in a
 * dialog, because it cannot be taken back from the window and the row it would
 * remove is the only way back to that conversation.
 */

import { useEffect, useRef, useState } from "react";

import { ClockIcon, DotsIcon, Modal, PencilIcon, TrashIcon } from "../../../shared/ui";

interface MenuProps {
  title: string;
  open: boolean;
  onOpen: (open: boolean) => void;
  onRename: () => void;
  onDelete: () => Promise<void>;
  /** Offered when the frame can take a person to the schedules page. */
  onRepeat?: () => void;
}

export function ThreadActions({ title, open, onOpen, onRename, onDelete, onRepeat }: MenuProps) {
  const [confirming, setConfirming] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const menu = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const away = (event: MouseEvent) => {
      if (menu.current && !menu.current.contains(event.target as Node)) onOpen(false);
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key === "Escape") onOpen(false);
    };
    document.addEventListener("mousedown", away);
    document.addEventListener("keydown", escape);
    return () => {
      document.removeEventListener("mousedown", away);
      document.removeEventListener("keydown", escape);
    };
  }, [open, onOpen]);

  return (
    <div className="thread-actions" ref={menu}>
      <button
        type="button"
        className="thread-more"
        aria-label={`Options for ${title || "this task"}`}
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={(event) => {
          event.stopPropagation();
          onOpen(!open);
        }}
      >
        <DotsIcon />
      </button>
      {open && (
        <div className="menu" role="menu">
          <button
            type="button"
            role="menuitem"
            onClick={() => {
              onOpen(false);
              onRename();
            }}
          >
            <PencilIcon />
            Rename
          </button>
          {onRepeat && (
            <button
              type="button"
              role="menuitem"
              onClick={() => {
                onOpen(false);
                onRepeat();
              }}
            >
              <ClockIcon />
              Repeat on a schedule…
            </button>
          )}
          <button
            type="button"
            role="menuitem"
            className="danger"
            onClick={() => {
              onOpen(false);
              setConfirming(true);
            }}
          >
            <TrashIcon />
            Delete
          </button>
        </div>
      )}
      {confirming && (
        <Modal
          title="Delete this task?"
          note="It leaves the list. Anything still running in it is stopped; what it already did stays on this machine."
          onClose={() => setConfirming(false)}
        >
          <div className="modal-actions">
            <button type="button" className="mini bordered" onClick={() => setConfirming(false)}>
              Cancel
            </button>
            <button
              type="button"
              className="mini danger-fill"
              disabled={deleting}
              onClick={async () => {
                setDeleting(true);
                try {
                  await onDelete();
                } finally {
                  setDeleting(false);
                  setConfirming(false);
                }
              }}
            >
              Delete
            </button>
          </div>
        </Modal>
      )}
    </div>
  );
}

interface TitleFieldProps {
  initial: string;
  onDone: (title: string | null) => void;
}

/** The row's title, editable. Enter or leaving the field keeps it; Escape does not. */
export function ThreadTitleField({ initial, onDone }: TitleFieldProps) {
  const [value, setValue] = useState(initial);
  const finished = useRef(false);
  const finish = (title: string | null) => {
    if (finished.current) return;
    finished.current = true;
    onDone(title);
  };
  return (
    <input
      className="thread-rename"
      aria-label="Task name"
      autoFocus
      value={value}
      onFocus={(event) => event.currentTarget.select()}
      onChange={(event) => setValue(event.target.value)}
      onKeyDown={(event) => {
        if (event.key === "Enter") finish(value.trim() || null);
        if (event.key === "Escape") finish(null);
      }}
      onBlur={() => finish(value.trim() || null)}
    />
  );
}
