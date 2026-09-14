import { useState } from "react";

/**
 * Forgetting one line, in two clicks.
 *
 * The second click is the same as a document's "Remove for good": nothing
 * brings a forgotten line back, and a listing is where people click while
 * reading.
 */
export function ForgetMemory({
  id,
  onForget,
}: {
  id: string;
  onForget: (id: string) => Promise<void>;
}) {
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);

  const forget = async () => {
    setBusy(true);
    try {
      await onForget(id);
    } finally {
      setBusy(false);
      setConfirming(false);
    }
  };

  return (
    <span className="actions">
      {confirming ? (
        <button type="button" className="danger" disabled={busy} onClick={() => void forget()}>
          Forget for good
        </button>
      ) : (
        <button type="button" disabled={busy} onClick={() => setConfirming(true)}>
          Forget
        </button>
      )}
    </span>
  );
}
