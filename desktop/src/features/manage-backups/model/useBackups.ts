/**
 * Making a backup, checking one, and handing one to the runtime to restore.
 *
 * Every check is the runtime's: whether a file is a complete backup, whether a
 * passphrase opens it, whether its schema can be read. The window carries the
 * answer. A restore is accepted only after the runtime has checked the whole
 * backup, and then happens while the runtime restarts; the window waits for the
 * new process and reloads, because nothing it holds describes the restored data.
 */

import { useCallback, useState } from "react";

import { report, type RuntimeClient } from "../../../shared/api";
import { describe } from "../../../shared/lib";
import { createBackup, requestRestore, verifyBackup, type BackupSummary } from "../api/backups";

export interface BackupsState {
  busy: boolean;
  problem: string;
  made: BackupSummary | null;
  checked: BackupSummary | null;
  restoring: boolean;
  make: (options: { destination?: string; passphrase?: string }) => Promise<void>;
  check: (path: string, passphrase?: string) => Promise<BackupSummary | null>;
  restore: (options: { path: string; passphrase?: string; withoutSecrets?: boolean }) => Promise<void>;
}

const wait = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

export function useBackups(
  client: RuntimeClient,
  { onRestored, pollMs = 1000, timeoutMs = 180_000 }: {
    onRestored?: () => void;
    pollMs?: number;
    timeoutMs?: number;
  } = {},
): BackupsState {
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState("");
  const [made, setMade] = useState<BackupSummary | null>(null);
  const [checked, setChecked] = useState<BackupSummary | null>(null);
  const [restoring, setRestoring] = useState(false);

  const failed = (error: unknown) => {
    const said = describe(error);
    setProblem(said);
    report(said);
  };

  const make = useCallback(
    async (options: { destination?: string; passphrase?: string }) => {
      setBusy(true);
      setProblem("");
      try {
        setMade(await createBackup(client, options));
      } catch (error) {
        failed(error);
      } finally {
        setBusy(false);
      }
    },
    [client],
  );

  const check = useCallback(
    async (path: string, passphrase?: string) => {
      setBusy(true);
      setProblem("");
      try {
        const found = await verifyBackup(client, { path, passphrase });
        setChecked(found);
        return found;
      } catch (error) {
        setChecked(null);
        failed(error);
        return null;
      } finally {
        setBusy(false);
      }
    },
    [client],
  );

  const restore = useCallback(
    async ({ path, passphrase, withoutSecrets }: {
      path: string;
      passphrase?: string;
      withoutSecrets?: boolean;
    }) => {
      setBusy(true);
      setProblem("");
      let before = "";
      try {
        before = (await client.get<{ started_at?: string }>("/api/health")).started_at ?? "";
        await requestRestore(client, { path, passphrase, without_secrets: withoutSecrets });
      } catch (error) {
        failed(error);
        setBusy(false);
        return;
      }
      setRestoring(true);
      const deadline = Date.now() + timeoutMs;
      while (Date.now() < deadline) {
        await wait(pollMs);
        try {
          const health = await client.get<{ started_at?: string }>("/api/health");
          if (health.started_at && health.started_at !== before) {
            (onRestored ?? (() => window.location.reload()))();
            return;
          }
        } catch {
          // The runtime is between processes; not answering is expected.
        }
      }
      setRestoring(false);
      setBusy(false);
      setProblem("Prometheus did not come back after the restore. Start it again and check.");
    },
    [client, onRestored, pollMs, timeoutMs],
  );

  return { busy, problem, made, checked, restoring, make, check, restore };
}
