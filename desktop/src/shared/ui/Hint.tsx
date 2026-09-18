/**
 * The mark beside a field that explains it, and says nothing until asked.
 *
 * A paragraph under every control is a form that reads like a manual: the
 * schedule form had two of them, each longer than the field it belonged to, so
 * the thing a person came to set was always below something they had read
 * before. The words are worth keeping - a free model overloading mid-run is
 * the commonest way a schedule fails quietly - they are just not worth reading
 * every time.
 *
 * Hover and keyboard focus both open it, and it is a `button` rather than a
 * marked-up glyph so that a keyboard reaches it at all. The text is in the
 * accessible name too, so a screen reader is told it rather than being handed
 * a question mark.
 */

import type { ReactNode } from "react";

export function Hint({ children, label }: { children: ReactNode; label: string }) {
  return (
    <span className="hint-mark">
      <button type="button" aria-label={label} className="hint-dot">
        ?
      </button>
      <span className="hint-bubble" role="tooltip">
        {children}
      </span>
    </span>
  );
}
