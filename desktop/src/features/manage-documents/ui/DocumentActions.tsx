import { useState } from "react";

import type { Document } from "../../../entities/document";

/**
 * What a person does to one document.
 *
 * **Update** is choosing a newer version of the file: the document keeps its
 * place and its title and takes the new text. It is the button people reached
 * for when "Re-index" was the only one there, expecting to pick a file.
 *
 * **Re-index** is offered only where the core says the document needs it -
 * text without vectors, because the embedding server was not there. It used to
 * be on every document for the case of a changed embedding model, and that
 * case now re-indexes itself.
 */
export function DocumentActions({
  document,
  busy: working = false,
  onUpdate,
  onReindex,
  onRemove,
}: {
  document: Document;
  /** The runtime is already doing something to this one: offer nothing else. */
  busy?: boolean;
  onUpdate: (id: string) => Promise<void>;
  onReindex: (id: string) => Promise<void>;
  onRemove: (id: string) => Promise<void>;
}) {
  const [mine, setMine] = useState(false);
  const busy = mine || working;
  const [confirming, setConfirming] = useState(false);

  const run = (action: (id: string) => Promise<void>) => async () => {
    setMine(true);
    try {
      await action(document.id);
    } finally {
      setMine(false);
      setConfirming(false);
    }
  };

  return (
    <span className="actions">
      <button type="button" disabled={busy} onClick={run(onUpdate)}>
        Update
      </button>
      {document.needs_indexing && (
        <button type="button" disabled={busy} onClick={run(onReindex)}>
          Re-index
        </button>
      )}
      {confirming ? (
        <button type="button" disabled={busy} onClick={run(onRemove)}>
          Remove for good
        </button>
      ) : (
        <button type="button" disabled={busy} onClick={() => setConfirming(true)}>
          Remove
        </button>
      )}
    </span>
  );
}
