import { useState } from "react";

import { chooseFiles } from "../../../shared/api";
import type { BackupSummary } from "../api/backups";

/**
 * Choose a backup, let the runtime check all of it, then restore.
 *
 * Two steps on purpose: what a person confirms is a backup the runtime has
 * already checked - complete, every checksum matching, a schema it can read -
 * with its date and version in front of them, not a file name.
 */
export function RestoreForm({
  checked,
  onCheck,
  onRestore,
  disabled,
}: {
  checked: BackupSummary | null;
  onCheck: (path: string, passphrase?: string) => Promise<BackupSummary | null>;
  onRestore: (options: { path: string; passphrase?: string; withoutSecrets?: boolean }) => Promise<void>;
  disabled?: boolean;
}) {
  const [path, setPath] = useState("");
  const [passphrase, setPassphrase] = useState("");
  const [withoutSecrets, setWithoutSecrets] = useState(false);
  const verified = checked !== null && checked.path === path;
  const needsPassphrase = verified && checked.includes_secrets && !withoutSecrets;

  return (
    <form
      className="backup-form"
      onSubmit={(event) => {
        event.preventDefault();
        if (!verified) {
          void onCheck(path, passphrase || undefined);
          return;
        }
        void onRestore({ path, passphrase: passphrase || undefined, withoutSecrets });
      }}
    >
      <label>
        <span>Backup file</span>
        <span className="backup-path">
          <input value={path} onChange={(event) => setPath(event.target.value)} />
          <button
            type="button"
            className="addbtn"
            onClick={() =>
              void chooseFiles({ title: "Choose a backup" }).then((chosen) => {
                if (chosen && chosen[0]) setPath(chosen[0]);
              })
            }
          >
            Choose…
          </button>
        </span>
      </label>
      {verified && (
        <p className="note" role="status">
          Complete and unchanged: made {new Date(checked.created_at).toLocaleString()} by version{" "}
          {checked.app_version || "unknown"}, schema {checked.schema_revision},{" "}
          {checked.includes_secrets ? "with sealed credentials" : "without credentials"}.
        </p>
      )}
      {needsPassphrase && (
        <label>
          <span>Passphrase</span>
          <input
            type="password"
            autoComplete="off"
            value={passphrase}
            onChange={(event) => setPassphrase(event.target.value)}
          />
        </label>
      )}
      {verified && checked.includes_secrets && (
        <label className="checkbox">
          <input
            type="checkbox"
            checked={withoutSecrets}
            onChange={(event) => setWithoutSecrets(event.target.checked)}
          />
          <span>Restore without credentials; reconnect services afterwards</span>
        </label>
      )}
      {verified && (
        <p className="note">
          Prometheus restarts to restore. Running work stops the safe way; this installation is
          kept beside the restored one until you delete it.
        </p>
      )}
        <button
          type="submit"
          className={verified ? "danger" : "primary"}
          disabled={disabled || path === "" || (needsPassphrase && passphrase === "")}
        >
          {verified ? "Restore and restart" : "Check backup"}
        </button>
    </form>
  );
}
