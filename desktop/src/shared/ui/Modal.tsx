/**
 * A form shown only once somebody has asked for it.
 *
 * Settings used to carry every form open at all times: adding a model meant
 * reading past four fields, a fieldset and a credential block that were on the
 * screen whether or not anybody was adding anything. The screen's job is to
 * show what is configured; asking for something new is a second, deliberate
 * act, and it gets its own surface.
 *
 * It decides nothing. Escape and the backdrop close it, and whether the work
 * behind it succeeded is the caller's to know - this component never guesses at
 * an outcome it cannot see.
 *
 * It is drawn into the body rather than where it was asked for. A `fixed`
 * element is positioned against the viewport only until some ancestor has a
 * filter, a transform or a backdrop - and the page header has all the blur it
 * needs to sit over a scrolling column, so the brief opened from it covered
 * fifty-two pixels of header and slid under the conversation. A dialog belongs
 * to the window, not to the corner it was opened from.
 */

import { useEffect, type ReactNode } from "react";
import { createPortal } from "react-dom";

import { CloseIcon } from "./icons";

interface Props {
  title: string;
  note?: string;
  onClose: () => void;
  children: ReactNode;
  /**
   * A form that is read across rather than down. One narrow column is right
   * for a dialog that asks one thing; a form with a timing block, two settings
   * and a description in it becomes a scroll through a column of unrelated
   * fields, and what the person came to set is always below what they did not.
   */
  wide?: boolean;
}

export function Modal({ title, note, onClose, children, wide = false }: Props) {
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  const dialog = (
    <div
      className="modal-scrim"
      // On mousedown rather than click, and only when the press started on the
      // backdrop itself: a drag that begins inside the form and ends outside it
      // is somebody selecting text, not somebody closing the dialog.
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div
        className={wide ? "modal wide" : "modal"}
        role="dialog"
        aria-modal="true"
        aria-label={title}
      >
        <header className="modal-head">
          <h2>{title}</h2>
          <button type="button" className="icobtn" aria-label="Close" onClick={onClose}>
            <CloseIcon />
          </button>
        </header>
        {note && <p className="note">{note}</p>}
        <div className="modal-body">{children}</div>
      </div>
    </div>
  );

  return typeof document === "undefined" ? dialog : createPortal(dialog, document.body);
}
