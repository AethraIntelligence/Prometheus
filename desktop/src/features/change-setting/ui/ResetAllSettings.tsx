import { useState } from "react";

/**
 * Every setting back to `.env` or the platform's default, in two clicks.
 *
 * Two because it forgets every choice made on this screen at once, and nothing
 * brings those back - the same rule a document's "Remove for good" follows.
 */
export function ResetAllSettings({
  onReset,
  disabled,
}: {
  onReset: () => Promise<void>;
  disabled?: boolean;
}) {
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);

  const reset = async () => {
    setBusy(true);
    try {
      await onReset();
    } finally {
      setBusy(false);
      setConfirming(false);
    }
  };

  return (
    <span className="actions">
      {confirming ? (
        <>
          <button type="button" disabled={busy} onClick={() => setConfirming(false)}>
            Cancel
          </button>
          <button type="button" className="danger" disabled={busy} onClick={() => void reset()}>
            Reset every setting
          </button>
        </>
      ) : (
        <button type="button" disabled={disabled || busy} onClick={() => setConfirming(true)}>
          Reset all to defaults
        </button>
      )}
    </span>
  );
}
