import { useState, type FormEvent } from "react";

/** The runtime's own limit, repeated only so the field can stop at it. */
const MAX_LENGTH = 2000;

/**
 * Something to keep, and the one question worth asking about it: is it true
 * of you everywhere, or of this workspace. Every other property of the note
 * has one right answer, and the core gives it.
 */
export function AddMemoryForm({
  onAdd,
  disabled,
}: {
  onAdd: (content: string, aboutThePerson: boolean) => Promise<void>;
  disabled?: boolean;
}) {
  const [content, setContent] = useState("");
  const [aboutThePerson, setAboutThePerson] = useState(false);
  const [busy, setBusy] = useState(false);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!content.trim()) return;
    setBusy(true);
    try {
      await onAdd(content.trim(), aboutThePerson);
      setContent("");
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className="add-integration" onSubmit={submit} aria-label="Remember something">
      <label>
        What to remember
        <textarea
          value={content}
          rows={4}
          maxLength={MAX_LENGTH}
          placeholder="Invoices for 2026 are in finance/2026"
          onChange={(event) => setContent(event.target.value)}
          disabled={disabled || busy}
        />
      </label>
      <label className="checkbox">
        <input
          type="checkbox"
          checked={aboutThePerson}
          onChange={(event) => setAboutThePerson(event.target.checked)}
          disabled={disabled || busy}
        />
        About me, in every workspace
      </label>
      <button type="submit" disabled={disabled || busy || !content.trim()}>
        {busy ? "Remembering…" : "Remember"}
      </button>
    </form>
  );
}
