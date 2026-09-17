import { useState, type FormEvent } from "react";

/** The runtime's own limit, repeated only so the field can stop at it. */
const MAX_LENGTH = 2000;

/**
 * What a line should say instead. The old wording is not lost: the core keeps
 * it as replaced, so a person can still see what a run last week was told.
 */
export function CorrectMemoryForm({
  current,
  onCorrect,
}: {
  current: string;
  onCorrect: (content: string) => Promise<void>;
}) {
  const [content, setContent] = useState(current);
  const [busy, setBusy] = useState(false);
  const unchanged = content.trim() === current.trim();

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!content.trim() || unchanged) return;
    setBusy(true);
    try {
      await onCorrect(content.trim());
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className="add-integration" onSubmit={submit} aria-label="Correct a memory">
      <label>
        What it should say
        <textarea
          value={content}
          rows={4}
          maxLength={MAX_LENGTH}
          onChange={(event) => setContent(event.target.value)}
          disabled={busy}
        />
      </label>
      <button type="submit" disabled={busy || !content.trim() || unchanged}>
        {busy ? "Correcting…" : "Correct"}
      </button>
    </form>
  );
}
