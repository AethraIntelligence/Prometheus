/**
 * Backups of this installation, and putting one back.
 *
 * A backup is a file a person asked for and keeps where they choose - not
 * synchronisation, and nothing reads it until it is restored. The screen says
 * what is in one and what is not, because "backup" alone reads as "everything"
 * and the credentials are deliberately not in it unless asked for.
 */

import { useState } from "react";

import { UpdatePanel } from "../../../../features/install-update";
import { BackupForm, RestoreForm, useBackups } from "../../../../features/manage-backups";
import { useRuntime } from "../../../../shared/api";
import { Modal, PlusIcon } from "../../../../shared/ui";

function size(bytes: number): string {
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function BackupsSection({ onRestored }: { onRestored?: () => void }) {
  const client = useRuntime();
  const backups = useBackups(client, { onRestored });
  const [making, setMaking] = useState(false);
  const [restoring, setRestoring] = useState(false);

  return (
    <>
      <p className="lede">
        A backup holds the history of your work, your settings and the files tasks produced. Stored
        credentials stay out unless you add them sealed with a passphrase, for moving to another
        computer.
      </p>

      {backups.problem && (
        <p className="problem" role="alert">
          {backups.problem}
        </p>
      )}
      {backups.restoring && (
        <p className="note" role="status">
          Restoring… the window reloads when Prometheus is back.
        </p>
      )}

      <section className="panel">
        <div className="panel-head">
          <h2>Backups</h2>
          <button
            type="button"
            className="addbtn"
            disabled={backups.busy}
            onClick={() => setMaking(true)}
          >
            <PlusIcon />
            Make a backup
          </button>
        </div>
        <div className="card">
          {backups.made ? (
            <p className="card-empty">
              Written to <code>{backups.made.path}</code> ({size(backups.made.bytes)},{" "}
              {backups.made.includes_secrets ? "credentials sealed inside" : "no credentials"}).
            </p>
          ) : (
            <p className="card-empty">No backup made in this session.</p>
          )}
        </div>
      </section>

      <section className="panel">
        <div className="panel-head">
          <h2>Restore</h2>
          <button
            type="button"
            className="addbtn"
            disabled={backups.busy}
            onClick={() => setRestoring(true)}
          >
            Restore from a backup…
          </button>
        </div>
      </section>

      <section className="panel">
        <UpdatePanel />
      </section>

      {making && (
        <Modal
          title="Make a backup"
          note="Safe while work is running."
          onClose={() => setMaking(false)}
        >
          <BackupForm
            disabled={backups.busy}
            onMake={async (options) => {
              await backups.make(options);
              setMaking(false);
            }}
          />
        </Modal>
      )}
      {restoring && (
        <Modal
          title="Restore from a backup"
          note="The whole backup is checked before anything changes."
          onClose={() => setRestoring(false)}
        >
          <RestoreForm
            checked={backups.checked}
            disabled={backups.busy}
            onCheck={backups.check}
            onRestore={backups.restore}
          />
        </Modal>
      )}
    </>
  );
}
