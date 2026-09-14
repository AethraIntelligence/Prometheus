/**
 * Adding a way in to a provider: a name you choose, a kind, and a key.
 *
 * The kinds come from the runtime rather than from a list written here - and no
 * vendor is named anywhere in this window, which `architecture.test.ts` enforces.
 * A window that offered a provider the backend cannot build a client for would
 * be a settings page that accepts something and fails four hours later inside a
 * task, and the list of adapters is not this layer's to know.
 *
 * The key is sent and dropped. It is never read back, never kept in state that
 * outlives the submit, and there is no endpoint that would return it.
 */

import { useState, type FormEvent } from "react";

import type { ProviderKind } from "../../../entities/provider";

export interface ConnectionSubmission {
  name: string;
  kind: string;
  apiKey: string;
  baseUrl: string;
}

interface Props {
  kinds: ProviderKind[];
  /** Filled in when the way here was a recommendation that already knows both. */
  initialKind?: string;
  initialName?: string;
  onAdd: (submission: ConnectionSubmission) => Promise<void>;
  disabled?: boolean;
}

export function AddConnectionForm({ kinds, onAdd, disabled, initialKind, initialName }: Props) {
  const [name, setName] = useState(initialName ?? "");
  // Empty until a person picks: the kinds arrive from the runtime after the
  // first render, and a value captured then would be "" for good - a form that
  // looks filled in and refuses to submit.
  const [picked, setKind] = useState(initialKind ?? "");
  const kind = picked || kinds[0]?.name || "";
  const [apiKey, setApiKey] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [busy, setBusy] = useState(false);

  const chosen = kinds.find((item) => item.name === kind);
  const needsKey = chosen?.needs_credential ?? true;

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!name.trim() || !kind || busy) return;
    setBusy(true);
    try {
      await onAdd({ name: name.trim(), kind, apiKey, baseUrl: baseUrl.trim() });
      setName("");
      setApiKey("");
      setBaseUrl("");
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className="add-integration" onSubmit={submit} aria-label="Add a provider">
      <label>
        Connection name
        <input
          value={name}
          onChange={(event) => setName(event.target.value)}
          placeholder="work-account"
          disabled={disabled || busy}
        />
      </label>
      <label>
        Provider
        <select
          value={kind}
          onChange={(event) => setKind(event.target.value)}
          disabled={disabled || busy}
        >
          {kinds.map((item) => (
            <option key={item.name} value={item.name}>
              {item.label}
            </option>
          ))}
        </select>
      </label>
      {needsKey && (
        <label>
          API key
          <input
            type="password"
            value={apiKey}
            onChange={(event) => setApiKey(event.target.value)}
            placeholder="stored encrypted; never shown again"
            disabled={disabled || busy}
          />
        </label>
      )}
      <label>
        Address <span className="hint">optional</span>
        <input
          value={baseUrl}
          onChange={(event) => setBaseUrl(event.target.value)}
          placeholder={chosen?.default_base_url || "the provider's own"}
          disabled={disabled || busy}
        />
      </label>
      <button type="submit" disabled={disabled || busy || !name.trim()}>
        {busy ? "Adding…" : "Add provider"}
      </button>
    </form>
  );
}
