import { isTauri } from "@tauri-apps/api/core";
import { relaunch } from "@tauri-apps/plugin-process";
import { check, type Update } from "@tauri-apps/plugin-updater";

/**
 * Asking the shell whether a newer, signed build exists, and installing it.
 *
 * The shell does the part that has to be trusted: it downloads the package
 * named by the update manifest, checks its signature against the public key
 * compiled into this build, and refuses anything that does not verify - a
 * corrupted, substituted or truncated download never replaces the running
 * version. A build made without a key has no updater at all, and says so.
 *
 * When to install is not decided here either: the runtime says whether an
 * effect is in flight (`/api/runtime/update-readiness`), and the feature above
 * this waits for its answer.
 */

export interface AvailableUpdate {
  version: string;
  currentVersion: string;
  notes: string;
  date?: string;
  install: (onProgress?: (downloaded: number, total: number | null) => void) => Promise<void>;
}

export type UpdateCheck =
  | { kind: "unsupported"; reason: string }
  | { kind: "current" }
  | { kind: "available"; update: AvailableUpdate };

export async function checkForUpdate(): Promise<UpdateCheck> {
  if (!isTauri()) {
    return { kind: "unsupported", reason: "Updates are installed by the desktop application." };
  }
  let found: Update | null;
  try {
    found = await check();
  } catch (error) {
    const said = String(error);
    if (/not (initialized|registered|found)|plugin updater/i.test(said)) {
      return { kind: "unsupported", reason: "This build was made without an update channel." };
    }
    throw error;
  }
  if (found === null) return { kind: "current" };
  const update = found;
  return {
    kind: "available",
    update: {
      version: update.version,
      currentVersion: update.currentVersion,
      notes: update.body ?? "",
      date: update.date,
      install: async (onProgress) => {
        let downloaded = 0;
        let total: number | null = null;
        await update.downloadAndInstall((event) => {
          if (event.event === "Started") total = event.data.contentLength ?? null;
          if (event.event === "Progress") downloaded += event.data.chunkLength;
          onProgress?.(downloaded, total);
        });
      },
    },
  };
}

export async function restartApplication(): Promise<void> {
  if (isTauri()) await relaunch();
}
