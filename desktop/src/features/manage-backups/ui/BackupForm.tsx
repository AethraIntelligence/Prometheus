import { useState } from "react";

import { chooseFolders } from "../../../shared/api";

/**
 * Where a backup goes, and whether it carries the key to stored credentials.
 *
 * Secrets are off by default and cost a passphrase to turn on, with the one
 * sentence that matters said before it is typed: anybody holding the file and
 * the passphrase can read every stored credential.
 */
export function BackupForm({
  onMake,
  disabled,
}: {
  onMake: (options: { destination?: string; passphrase?: string }) => Promise<void>;
  disabled?: boolean;
}) {
  const [folder, setFolder] = useState("");
  const [withSecrets, setWithSecrets] = useState(false);
  const [passphrase, setPassphrase] = useState("");
  const [again, setAgain] = useState("");

  const mismatch = withSecrets && again !== "" && passphrase !== again;
  const ready = !disabled && (!withSecrets || (passphrase !== "" && passphrase === again));

  return (
    <form
      className="backup-form"
      onSubmit={(event) => {
        event.preventDefault();
        if (!ready) return;
        const destination = folder
          ? `${folder.replace(/[\\/]$/, "")}/Prometheus backup ${new Date()
              .toISOString()
              .slice(0, 19)
              .replace("T", " ")
              .replaceAll(":", "")}.zip`
          : undefined;
        void onMake({ destination, passphrase: withSecrets ? passphrase : undefined });
      }}
    >
      <label>
        <span>Folder</span>
        <span className="backup-path">
          <input
            value={folder}
            placeholder="The default backups folder"
            onChange={(event) => setFolder(event.target.value)}
          />
          <button
            type="button"
            className="addbtn"
            onClick={() =>
              void chooseFolders({ title: "Where to keep the backup" }).then((chosen) => {
                if (chosen && chosen[0]) setFolder(chosen[0]);
              })
            }
          >
            Choose…
          </button>
        </span>
      </label>
      <label className="checkbox">
        <input
          type="checkbox"
          checked={withSecrets}
          onChange={(event) => setWithSecrets(event.target.checked)}
        />
        <span>Include stored credentials, sealed with a passphrase, to move to another computer</span>
      </label>
      {withSecrets && (
        <>
          <p className="note">
            Anyone holding this file and its passphrase can read every stored credential. Use at
            least 12 characters.
          </p>
          <label>
            <span>Passphrase</span>
            <input
              type="password"
              autoComplete="new-password"
              value={passphrase}
              onChange={(event) => setPassphrase(event.target.value)}
            />
          </label>
          <label>
            <span>Passphrase again</span>
            <input
              type="password"
              autoComplete="new-password"
              value={again}
              onChange={(event) => setAgain(event.target.value)}
            />
          </label>
          {mismatch && <p className="problem-inline">The passphrases differ.</p>}
        </>
      )}
        <button type="submit" className="primary" disabled={!ready}>
          Make backup
        </button>
    </form>
  );
}
