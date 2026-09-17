import type { RuntimeClient } from "../../../shared/api";

/** What the runtime says about a backup it made or checked. */
export interface BackupSummary {
  path: string;
  created_at: string;
  app_version: string;
  schema_revision: string;
  includes_secrets: boolean;
  bytes: number;
  entries: Record<string, number>;
  secrets_unlocked?: boolean;
}

export async function createBackup(
  client: RuntimeClient,
  body: { destination?: string; passphrase?: string },
): Promise<BackupSummary> {
  return client.post<BackupSummary>("/api/backups", body);
}

export async function verifyBackup(
  client: RuntimeClient,
  body: { path: string; passphrase?: string },
): Promise<BackupSummary> {
  return client.post<BackupSummary>("/api/backups/verify", body);
}

export async function requestRestore(
  client: RuntimeClient,
  body: { path: string; passphrase?: string; without_secrets?: boolean },
): Promise<{ restoring: boolean; stopping: number; backup: BackupSummary }> {
  return client.post("/api/runtime/restore", body);
}
